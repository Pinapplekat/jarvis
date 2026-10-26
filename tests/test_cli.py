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
    assert str(data_dir) in out and "[1, 2, 3]" in out and "Falak" in out
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
