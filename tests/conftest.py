import pytest


@pytest.fixture(autouse=True)
def _no_real_status_writes(monkeypatch, tmp_path):
    """Keep tests from touching the real menu bar status file."""
    from jarvis import status
    monkeypatch.setattr(status, "PATH", tmp_path / "status-test.json")
