from datetime import datetime
from zoneinfo import ZoneInfo

from jarvis.canvas import (
    from_feed, from_memory, import_deadlines, merge, parse_due, raise_deadline_alerts,
)
from jarvis.memory import Memory
from tests.test_canvas import FEED

TZ = ZoneInfo("America/Los_Angeles")


def test_parse_due_formats():
    assert parse_due("2026-10-18", TZ) == datetime(2026, 10, 18, 23, 59, tzinfo=TZ)
    assert parse_due("2026-10-18 09:30", TZ) == datetime(2026, 10, 18, 9, 30, tzinfo=TZ)
    assert parse_due("2026-10-18T16:30:00+00:00", TZ).astimezone(TZ).hour == 9


def test_import_overrides_feed_copy_by_canvas_key():
    m = Memory(":memory:")
    import_deadlines(m, [
        {"course": "CMST&101", "title": "Weekly Homework 3", "due": "2026-10-18",
         "key": "canvas:901"},
        {"course": "ECON 100", "title": "Module 3: Chapter 5", "due": "2026-10-18"},
    ], TZ)
    merged = merge(from_feed(FEED, TZ), from_memory(m))
    titles = [d.title for d in merged if d.kind == "due"]
    assert "Weekly Homework 4" not in titles
    assert {"Weekly Homework 3", "Module 3: Chapter 5", "Lyric Essay Draft"} <= set(titles)


def test_reimport_keeps_done_unless_told_otherwise():
    m = Memory(":memory:")
    item = {"course": "ECON 100", "title": "Module 3", "due": "2026-10-18"}
    import_deadlines(m, [item], TZ)
    (row,) = m.deadlines()
    assert m.set_deadline_done(row["key"])
    import_deadlines(m, [{**item, "due": "2026-10-20"}], TZ)  # extension: new date, new key
    import_deadlines(m, [item], TZ)  # same week re-run
    assert {r["due"][:10]: r["done"] for r in m.deadlines()} == {"2026-10-18": 1, "2026-10-20": 0}
    import_deadlines(m, [{**item, "done": False}], TZ)
    assert {r["due"][:10]: r["done"] for r in m.deadlines()}["2026-10-18"] == 0


def test_done_deadlines_dont_alert():
    m = Memory(":memory:")
    now = datetime(2026, 10, 18, 12, 0, tzinfo=TZ)
    import_deadlines(m, [
        {"course": "ECON 100", "title": "Module 3", "due": "2026-10-18"},
        {"course": "CMST&101", "title": "HW 3", "due": "2026-10-18", "done": True},
    ], TZ)
    assert raise_deadline_alerts(m, from_memory(m), now) == 1
    (alert,) = m.undelivered_alerts()
    assert alert["level"] == "high" and "Module 3" in alert["message"]
