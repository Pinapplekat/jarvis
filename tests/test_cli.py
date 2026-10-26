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
    assert str(data_dir) in out and "[1]" in out and "Falak" in out
    assert "SECRET_KEY" in out and "hunter2" not in out


def test_import_db_refuses_existing(data_dir, tmp_path, capsys):
    src = tmp_path / "src.db"
    sqlite3.connect(src).close()
    assert cli.main(["dev", "import-db", "--source", str(src)]) == 0
    assert cli.main(["dev", "import-db", "--source", str(src)]) == 1
    assert "already exists" in capsys.readouterr().err
    assert cli.main(["dev", "import-db", "--source", str(src), "--overwrite"]) == 0
