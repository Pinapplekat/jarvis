import pytest


@pytest.fixture(autouse=True)
def _no_real_status_writes(monkeypatch, tmp_path):
    """Keep tests from touching the real menu bar status file."""
    from jarvis import status
    monkeypatch.setattr(status, "PATH", tmp_path / "status-test.json")


@pytest.fixture(autouse=True)
def _no_real_geocoding(monkeypatch):
    """Never send a real reverse-geocode request from tests (the feature's tests turn it on with a fake)."""
    from jarvis import config, location
    monkeypatch.setattr(config, "REVERSE_GEOCODE", False)
    real = location.reverse_geocode
    monkeypatch.setattr(location, "reverse_geocode", lambda lat, lon, get=None: real(lat, lon, get=get) if get else None)


@pytest.fixture(autouse=True)
def _no_real_logs_or_state(monkeypatch, tmp_path):
    """Tests never write to JARVIS's real command log, task folders, build state or browser list."""
    from jarvis import browser, selfedit, tasks, terminal
    monkeypatch.setattr(terminal, "AUDIT", tmp_path / "commands-test.jsonl")
    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / "tasks-test")
    monkeypatch.setattr(selfedit, "BUILD_DIR", tmp_path / "build-test")
    monkeypatch.setattr(selfedit, "STATUS", tmp_path / "build-test" / "status.json")
    monkeypatch.setattr(selfedit, "LOG", tmp_path / "selfedit-test.log")
    monkeypatch.setattr(browser, "SITES", tmp_path / "browser-sites-test.json")


@pytest.fixture(autouse=True)
def _no_real_claude_sessions(monkeypatch, tmp_path):
    """The helpers view reads a fake transcripts folder in tests, never ~/.claude."""
    from jarvis import attachments, helpers
    monkeypatch.setattr(helpers, "PROJECTS", tmp_path / "claude-projects")
    monkeypatch.setattr(helpers, "screen_names", lambda: {})
    monkeypatch.setattr(helpers, "_cache", {})
    monkeypatch.setattr(attachments, "SHOTS_DIR", tmp_path / "shots")
