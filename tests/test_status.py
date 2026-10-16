import json
import time

import pytest

from jarvis import config, status


@pytest.fixture(autouse=True)
def tmp_status(monkeypatch, tmp_path):
    monkeypatch.setattr(status, "PATH", tmp_path / "status.json")


def test_states_per_source_with_expiry():
    status.set_state("ears", "waiting", until=time.time() + 30)
    status.set_state("telegram", "working", detail="answering")
    status.set_state("telegram", "idle")
    data = status.read()
    assert data["ears"]["state"] == "waiting" and data["ears"]["until"] > time.time()
    assert data["telegram"] == {**data["telegram"], "state": "idle", "until": None}


def test_busy_states_expire_by_default():
    status.set_state("ears", "working")
    assert 0 < status.read()["ears"]["until"] - time.time() <= status.BUSY_TTL


def test_file_is_always_valid_json():
    for i in range(20):
        status.set_state("voice", "speaking", until=time.time() + i)
        json.loads(status.PATH.read_text())
    assert not list(status.PATH.parent.glob(".status-*"))  # no temp files left behind


def test_speech_seconds_scales_with_words(monkeypatch):
    monkeypatch.setattr(config, "VOICE_RATE", 180)
    assert status.speech_seconds("Sir.") < status.speech_seconds("word " * 30) < 12
