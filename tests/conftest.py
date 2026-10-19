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
    from jarvis import browser, selfedit, speech, tasks, terminal
    monkeypatch.setattr(speech, "LIVE", tmp_path / "speaking-test.json")
    monkeypatch.setattr(speech, "CONVO", tmp_path / "conversation-test.json")
    from jarvis import hud
    monkeypatch.setattr(hud, "HUD_DIR", tmp_path / "hud-test")
    monkeypatch.setattr(hud, "REQUEST", tmp_path / "hud-test" / "request.json")
    monkeypatch.setattr(hud, "REPLY", tmp_path / "hud-test" / "reply.json")
    from jarvis import panel
    monkeypatch.setattr(panel, "PANEL_DIR", tmp_path / "panel-test")
    monkeypatch.setattr(panel, "STATE", tmp_path / "panel-test" / "state.json")
    monkeypatch.setattr(panel, "ACK", tmp_path / "panel-test" / "ack.json")
    monkeypatch.setattr(panel, "REQUEST", tmp_path / "panel-test" / "request.json")
    from jarvis import voice
    monkeypatch.setattr(voice, "ANNOUNCE_PID", tmp_path / "announcing-test.pid")
    monkeypatch.setattr(terminal, "AUDIT", tmp_path / "commands-test.jsonl")
    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / "tasks-test")
    from jarvis import inbox
    monkeypatch.setattr(inbox, "INBOX_DIR", tmp_path / "inbox-test")
    monkeypatch.setattr(inbox, "FEED", tmp_path / "inbox-test" / "feed.json")
    monkeypatch.setattr(inbox, "REQUESTS", tmp_path / "inbox-test" / "requests")
    from jarvis import jobs
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path / "jobs-test")
    monkeypatch.setattr(selfedit, "BUILD_DIR", tmp_path / "build-test")
    monkeypatch.setattr(selfedit, "STATUS", tmp_path / "build-test" / "status.json")
    monkeypatch.setattr(selfedit, "LOG", tmp_path / "selfedit-test.log")
    monkeypatch.setattr(browser, "SITES", tmp_path / "browser-sites-test.json")


class FakeKeychain:
    def __init__(self):
        self.items = {}

    def get(self, account):
        return self.items.get(account, "")

    def set(self, account, secret):
        self.items[account] = secret
        return True


@pytest.fixture(autouse=True)
def keychain(monkeypatch):
    """Tests never read or write the real Keychain; Spotify tokens go to an in-memory stand-in."""
    from jarvis import spotify
    fake = FakeKeychain()
    monkeypatch.setattr(spotify, "keychain", fake)
    return fake


@pytest.fixture(autouse=True)
def _no_real_claude_sessions(monkeypatch, tmp_path):
    """The helpers view reads a fake transcripts folder in tests, never ~/.claude."""
    from jarvis import attachments, helpers
    monkeypatch.setattr(helpers, "PROJECTS", tmp_path / "claude-projects")
    monkeypatch.setattr(helpers, "screen_names", lambda: {})
    monkeypatch.setattr(helpers, "_cache", {})
    monkeypatch.setattr(attachments, "SHOTS_DIR", tmp_path / "shots")
