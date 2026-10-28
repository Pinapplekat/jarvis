"""`jarvis browser setup|status|reset`: no Chromium is downloaded and no real folder is touched."""

import stat

from jarvis import browser_cli
from jarvis.apps.browser.sites import Sites


def test_setup_refuses_when_the_disk_is_nearly_full(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FALAK_NODE_DIR", str(tmp_path / "node"))
    monkeypatch.setattr(browser_cli, "_free", lambda path: 1 * 1024**3)
    ran = []
    monkeypatch.setattr(browser_cli.subprocess, "run", lambda *a, **k: ran.append(a))
    assert browser_cli.setup() == 1 and ran == []
    assert "1.0 GB free" in capsys.readouterr().err


def test_setup_installs_into_the_nodes_own_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("FALAK_NODE_DIR", str(tmp_path / "node"))
    monkeypatch.setattr(browser_cli, "_free", lambda path: 50 * 1024**3)
    seen = {}

    def fake_run(cmd, env):
        seen["cmd"], seen["path"] = cmd, env["PLAYWRIGHT_BROWSERS_PATH"]
        return type("Done", (), {"returncode": 0})()

    monkeypatch.setattr(browser_cli.subprocess, "run", fake_run)
    assert browser_cli.setup() == 0
    assert seen["cmd"][1:] == ["-m", "playwright", "install", "chromium"]
    assert seen["path"] == str(tmp_path / "node" / "browser" / "chromium")


def test_status_and_reset(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FALAK_NODE_DIR", str(tmp_path / "node"))
    sites = Sites(tmp_path / "node" / "browser")
    sites.make_profile()
    sites.add("canvas.test")
    (sites.profile / "Cookies").write_text("session")
    assert stat.S_IMODE(sites.profile.stat().st_mode) == 0o700
    assert browser_cli.status() == 0 and "canvas.test" in capsys.readouterr().out
    assert browser_cli.reset(read=lambda prompt: "no") == 1 and sites.profile.exists()  # only a typed "reset" does it
    assert browser_cli.reset(read=lambda prompt: "reset") == 0
    assert not sites.profile.exists() and sites.listed() == []
    assert browser_cli.reset(read=lambda prompt: "reset") == 0 and "Nothing to reset" in capsys.readouterr().out
