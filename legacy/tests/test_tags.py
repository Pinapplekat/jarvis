from datetime import datetime, timedelta, timezone

from jarvis import tags
from jarvis.judge import Answer, JevError
from jarvis.memory import Memory

T0 = datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc)


def test_guess_from_app_and_title():
    assert tags.guess("Code", "cart.py — shop-site") == "coding"
    assert tags.guess("Google Chrome", "Module 3 - ECON 100 - Canvas") == "homework"
    assert tags.guess("Google Chrome", "Lofi beats - YouTube") == "video"
    assert tags.guess("Discord", "#class-discussion") == "chatting"
    assert tags.guess("Safari", "Some blog") == "browsing"
    assert tags.guess("Calculator", None) == "other"


def test_jev_tagging_with_fallbacks():
    pick = lambda value: (lambda s, qs: {"activity": Answer(value, 0.9)})  # noqa: E731
    assert tags.tag_window("Google Chrome", "Docs", "Essay draft...", pick("writing")) == "writing"
    assert tags.tag_window("Discord", "#x", "", pick("nonsense")) == "chatting"   # invalid -> guess

    def broken(s, qs):
        raise JevError("down")
    assert tags.tag_window("Code", "a.py", "", broken) == "coding"
    assert tags.tag_window("Code", "a.py", "", None) == "coding"


def test_time_by_tag_caps_long_gaps():
    m = Memory(":memory:")
    m.observe("screen", "a", app="Code", tag="coding", at=T0)
    m.observe("screen", "b", app="Chrome", tag="homework", at=T0 + timedelta(minutes=5))
    m.observe("screen", "c", app="Discord", tag="chatting", at=T0 + timedelta(hours=2))  # 115 min later: capped
    m.observe("terminal", "$ ls  [ok, 0.1s]", at=T0 + timedelta(minutes=1))           # not screen time
    totals = tags.time_by_tag(m.observations(T0 - timedelta(minutes=1)), T0 + timedelta(hours=2, minutes=3))
    assert totals == {"coding": timedelta(minutes=5), "homework": timedelta(minutes=10),
                      "chatting": timedelta(minutes=3)}
    assert tags.describe(totals) == "homework 10m, coding 5m, chatting 3m"
    assert tags.describe({"coding": timedelta(minutes=130)}) == "coding 2h10m"
    assert tags.describe({}) == "(no screen time recorded)"
