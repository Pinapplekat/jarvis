import sqlite3

from jarvis import cli


def test_version(data_dir, capsys):
    try:
        cli.main(["--version"])
    except SystemExit as e:
        assert e.code == 0
    assert capsys.readouterr().out.strip() == "jarvis 0.2.0.dev0 (Falak)"


def test_dev_info(data_dir, capsys):
    data_dir.mkdir()
    (data_dir / ".env").write_text("SECRET_KEY=hunter2\n")
    assert cli.main(["dev", "info"]) == 0
    out = capsys.readouterr().out
    assert str(data_dir) in out and "[1, 2, 3, 4]" in out and "Falak" in out
    assert "SECRET_KEY" in out and "hunter2" not in out
    assert "settings for missing apps: none" in out


def test_dev_info_lists_settings_for_missing_apps(data_dir, capsys):
    data_dir.mkdir()
    (data_dir / "settings.toml").write_text("[apps.calcualtor]\nprecision = 5\n")
    assert cli.main(["dev", "info"]) == 0
    assert "settings for missing apps: ['calcualtor']" in capsys.readouterr().out


def test_import_db_refuses_existing(data_dir, tmp_path, capsys):
    src = tmp_path / "src.db"
    sqlite3.connect(src).close()
    assert cli.main(["dev", "import-db", "--source", str(src)]) == 0
    assert cli.main(["dev", "import-db", "--source", str(src)]) == 1
    assert "already exists" in capsys.readouterr().err
    assert cli.main(["dev", "import-db", "--source", str(src), "--overwrite"]) == 0


def test_apps_lists_real_apps(data_dir, capsys):
    assert cli.main(["apps"]) == 0
    out = capsys.readouterr().out
    assert "calculator" in out and "take_photo [read]" in out and "remember [local]" in out
    assert "node:camera" in out


def test_run_calculator_and_bad_json(data_dir, capsys):
    assert cli.main(["run", "calculator.evaluate", "--args", '{"expression": "2**10 + sqrt(16)"}']) == 0
    assert capsys.readouterr().out.strip() == "1028"
    assert cli.main(["run", "calculator.evaluate", "--args", "{nope"]) == 2
    assert cli.main(["run", "calculator.evaluate", "--args", '{"expression": "1/0"}']) == 1
    assert "divides by zero" in capsys.readouterr().err


def test_remember_then_undo(data_dir, capsys):
    cli.main(["run", "memory.remember", "--args", '{"category":"personal","key":"test","value":"x"}'])
    assert cli.main(["undo"]) == 0
    cli.main(["run", "memory.recall", "--args", '{"query":"test"}'])
    assert "No facts match" in capsys.readouterr().out
    assert cli.main(["undo"]) == 1  # nothing left to undo


def test_ask_flow_and_log(data_dir, capsys, monkeypatch):
    from pathlib import Path

    from jarvis.core.apps import load_apps

    fixtures = Path(__file__).parent / "fixtures" / "apps"
    monkeypatch.setattr(cli, "load_apps", lambda s: load_apps(s, fixtures))
    cli.main(["run", "echo.send", "--args", '{"text":"hi","to":"Sam"}'])
    assert "Asked #1: Send hi to Sam?" in capsys.readouterr().out
    cli.main(["asks"])
    assert "#1 Send hi to Sam?" in capsys.readouterr().out
    assert cli.main(["yes", "1"]) == 0
    assert "Sent hi to Sam" in capsys.readouterr().out
    assert cli.main(["yes", "1"]) == 1
    cli.main(["run", "echo.wipe", "--args", '{"what":"x"}'])
    assert cli.main(["no", "2"]) == 0
    assert "Declined #2" in capsys.readouterr().out
    cli.main(["log", "-n", "3"])
    assert "declined" in capsys.readouterr().out


def test_attachment_is_printed_then_deleted(data_dir, capsys, monkeypatch):
    from pathlib import Path

    from jarvis.core.apps import load_apps

    monkeypatch.setattr(cli, "load_apps", lambda s: load_apps(s, Path(__file__).parent / "fixtures" / "apps"))
    cli.main(["run", "echo.photo"])
    out = capsys.readouterr().out
    assert "image: 4 bytes at" in out
    path = Path(out.split(" at ")[1].strip())
    assert not path.exists()


def test_apps_lists_session_tools(data_dir, capsys):
    cli.main(["apps"])
    out = capsys.readouterr().out
    assert "live_view_start [read]" in out and "live_view_look [read]" in out and "screen" in out


def test_node_pair_prints_the_key_once_and_stores_a_hash(data_dir, capsys):
    from jarvis.core import db, nodes
    from jarvis.core.settings import load_settings

    assert cli.main(["node", "pair", "mac"]) == 0
    out = capsys.readouterr().out
    key = out.split("FALAK_NODE_KEY=")[1].strip()
    conn = db.connect(load_settings())
    (row,) = conn.execute("SELECT * FROM nodes").fetchall()
    assert row["key_hash"] == nodes.hash_key(key) and key not in tuple(row)
    assert cli.main(["node", "pair", "mac"]) == 1
    assert "already" in capsys.readouterr().err
    assert cli.main(["node", "list"]) == 0
    listed = capsys.readouterr().out
    assert "mac [offline]" in listed and key not in listed
    assert cli.main(["nodes"]) == 0
    assert "No node is connected" in capsys.readouterr().out
    assert cli.main(["node", "revoke", "mac"]) == 0
    assert cli.main(["node", "revoke", "mac"]) == 1
    cli.main(["node", "list"])
    assert "mac [revoked]" in capsys.readouterr().out


def test_nodes_lists_connected_ones_with_capabilities(data_dir, capsys):
    from jarvis.core import db, nodes
    from jarvis.core.settings import load_settings

    conn = db.connect(load_settings())
    nodes.pair(conn, "mac")
    conn.execute("UPDATE nodes SET connected_at='2026-10-26T10:00:00+00:00', capabilities_json='[\"camera\",\"screen\"]'")
    assert cli.main(["nodes"]) == 0
    assert "mac: capabilities camera, screen" in capsys.readouterr().out
    cli.main(["dev", "info"])
    assert "1 connected" in capsys.readouterr().out


def test_node_run_without_a_key_explains(data_dir, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FALAK_NODE_DIR", str(tmp_path / "node"))
    assert cli.main(["node", "run"]) == 1
    assert "jarvis node pair" in capsys.readouterr().err
