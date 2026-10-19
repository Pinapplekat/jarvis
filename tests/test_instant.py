from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from jarvis import cli, config, siri
from jarvis.memory import Memory
from jarvis.router import instant_command


@pytest.mark.parametrize("text,expected", [
    ("set a timer for 5 minutes", ("timer", 300)),
    ("Hey Jarvis, timer 90 seconds", ("timer", 90)),
    ("ten minute timer", ("timer", 600)),
    ("wake me up at 7:30 am", ("alarm", "07:30")),
    ("set an alarm for 18:15", ("alarm", "18:15")),
    ("what time is it?", ("time",)),
    ("what day is it", ("date",)),
    ("play some Daft Punk", ("play", "Daft Punk")),
    ("Jarvis, play study lo-fi please", ("play", "study lo-fi")),
])
def test_instant_commands(text, expected):
    assert instant_command(text) == expected


@pytest.mark.parametrize("text", ["play it", "play", "play the video on youtube", "set an alarm for 6",
                                  "what time is my econ class", "play some music and tell me about Econ " * 4])
def test_not_instant(text):
    assert instant_command(text) is None


def test_play_goes_straight_to_spotify_and_says_the_real_result(monkeypatch):
    monkeypatch.setattr(config, "SIRI", True)
    monkeypatch.setattr(siri, "music", lambda mem, q, kind: f"Playing {q} on Spotify.")
    monkeypatch.setattr(cli, "_runner_for", lambda *a: (_ for _ in ()).throw(AssertionError("no model")))
    m = Memory(":memory:")
    assert cli._answer(m, "play some Daft Punk", spoken=True) == "Playing Daft Punk on Spotify."
    assert [h["role"] for h in m.chat_history(5)] == ["user", "assistant"]


def test_a_failed_instant_play_says_so(monkeypatch):
    monkeypatch.setattr(config, "SIRI", True)

    def broken(mem, q, kind):
        raise ValueError("no active device")
    monkeypatch.setattr(siri, "music", broken)
    reply = cli._answer(Memory(":memory:"), "play some Daft Punk")
    assert reply.startswith("Sorry, sir, I couldn't play that") and "Playing" not in reply


def test_the_time_needs_no_model(monkeypatch):
    monkeypatch.setattr(cli, "_now_local", lambda: datetime(2026, 10, 19, 14, 5, tzinfo=ZoneInfo("America/Los_Angeles")))
    assert cli._answer(Memory(":memory:"), "what time is it") == "It's 2:05 PM, sir."
