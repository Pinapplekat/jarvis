from datetime import datetime

import pytest

from jarvis import config
from jarvis.memory import Memory
from jarvis.voice import Announcer, should_speak, speakable, user_present

NOON = datetime(2026, 10, 15, 12, 0)
NIGHT = datetime(2026, 10, 15, 23, 30)


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setattr(config, "VOICE", True)
    monkeypatch.setattr(config, "QUIET_HOURS", "23-7")
    monkeypatch.setattr(config, "VOICE_IDLE_SECONDS", 180)


class FakeMessenger:
    def __init__(self):
        self.sent = []

    def send(self, text, buttons=None):
        self.sent.append(text)


def test_speakable_strips_emoji_links_and_dashes():
    assert speakable("⏰ It's nine, sir.  Econ time 🚨") == "It's nine, sir. Econ time"
    assert speakable("see https://x.test/a?b=1 now") == "see the link now"
    assert speakable("Due:\n- HW 3\n- Quiz") == "Due: HW 3 Quiz"
    long = "Sentence one. " * 60
    out = speakable(long)
    assert len(out) <= 400 and out.endswith(".")


def test_presence_and_quiet_hours(monkeypatch):
    assert user_present(30) and not user_present(600)
    monkeypatch.setattr("jarvis.voice.idle_seconds", lambda: None)  # couldn't read idle time
    assert not user_present()
    assert should_speak(NOON, idle=10)
    assert not should_speak(NIGHT, idle=10)
    assert not should_speak(NOON, idle=900)


def test_announcer_always_texts_and_speaks_only_when_present():
    m, msg, spoken = Memory(":memory:"), FakeMessenger(), []
    at_desk = Announcer(msg, m, now=lambda: NOON, speaker=lambda t: spoken.append(t) or True,
                        presence=lambda now: True)
    away = Announcer(msg, m, now=lambda: NOON, speaker=lambda t: spoken.append(t) or True,
                     presence=lambda now: False)
    at_desk.send("📧 Dr. G: Module 3 extended")
    away.send("🔔 Rent due tomorrow")
    assert msg.sent == ["📧 Dr. G: Module 3 extended", "🔔 Rent due tomorrow"]
    assert spoken == ["📧 Dr. G: Module 3 extended"]
    assert m.recent_events()[0]["content"] == "Dr. G: Module 3 extended"


def test_voice_off_never_speaks(monkeypatch):
    monkeypatch.setattr(config, "VOICE", False)
    assert not should_speak(NOON, idle=1)
