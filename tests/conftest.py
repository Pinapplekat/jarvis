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
