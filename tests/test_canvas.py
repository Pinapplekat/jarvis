from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from jarvis.canvas import (
    format_digest, from_assignments, from_feed, from_modules, raise_deadline_alerts,
)
from jarvis.memory import Memory

TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=TZ)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _assignment(id, name, due, state="unsubmitted", types=("online_upload",), missing=False):
    return {
        "id": id, "name": name, "due_at": _iso(due) if due else None,
        "html_url": f"https://canvas.test/a/{id}", "submission_types": list(types),
        "submission": {"workflow_state": state, "missing": missing},
    }


def test_assignment_status_and_trackability():
    ds = from_assignments("CMST&101", [
        _assignment(1, "HW 3", NOW + timedelta(days=1)),
        _assignment(2, "HW 2", NOW - timedelta(days=3), missing=True),
        _assignment(3, "Quiz", NOW + timedelta(days=2), state="graded"),
        _assignment(4, "Contract", NOW + timedelta(days=2), types=("none",)),
        _assignment(5, "Undated", None),
    ])
    assert [(d.title, d.status, d.trackable) for d in ds] == [
        ("HW 3", "unsubmitted", True),
        ("HW 2", "missing", True),
        ("Quiz", "graded", True),
        ("Contract", "unknown", False),
    ]


def test_module_title_deadlines():
    modules = [
        {"id": 10, "name": "Module 2: Chapters 3 & 4 (DUE OCT 11)"},
        {"id": 11, "name": "Module 3: Chapter 5 (Due Oct. 18)"},
        {"id": 12, "name": "Module 9: Final (DUE JAN 3)"},
        {"id": 13, "name": "Start Here"},
    ]
    ds = from_modules("ECON100", 47310, modules, TZ, NOW.date())
    assert [(d.title, d.due.date()) for d in ds] == [
        ("Module 2: Chapters 3 & 4", date(2026, 10, 11)),
        ("Module 3: Chapter 5", date(2026, 10, 18)),
        ("Module 9: Final", date(2027, 1, 3)),
    ]
    assert ds[1].due.hour == 23 and ds[1].due.minute == 59 and not ds[1].trackable


def test_alert_thresholds():
    m = Memory(":memory:")
    ds = from_assignments("CMST", [
        _assignment(1, "Due in 2h", NOW + timedelta(hours=2)),
        _assignment(2, "Due in 20h", NOW + timedelta(hours=20)),
        _assignment(3, "Due in 2 days", NOW + timedelta(days=2)),
        _assignment(4, "Done, due in 1h", NOW + timedelta(hours=1), state="submitted"),
        _assignment(5, "Journal in 1h", NOW + timedelta(hours=1), types=("none",)),
        _assignment(6, "Past", NOW - timedelta(hours=1)),
    ])
    assert raise_deadline_alerts(m, ds, NOW) == 3
    alerts = {a["message"].split(":")[1].split(" is due")[0].split(" due")[0].strip(): a["level"]
              for a in m.undelivered_alerts()}
    assert alerts == {"Due in 2h": "urgent", "Due in 20h": "high", "Journal in 1h": "high"}
    assert raise_deadline_alerts(m, ds, NOW) == 0


FEED = b"""BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:-//Instructure//Canvas//EN\r
BEGIN:VEVENT\r
UID:event-assignment-901\r
DTSTART:20261016T065900Z\r
DTEND:20261016T065900Z\r
SUMMARY:Weekly Homework 4 [CMST& 101 - Intro to Communication - Fall 2026]\r
URL:https://canvas.seattlecolleges.edu/calendar?include_contexts=course_1&month=10\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:event-assignment-902\r
DTSTART;VALUE=DATE:20261018\r
SUMMARY:Lyric Essay Draft [ENGL&101 26 35938]\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:event-calendar-event-77\r
DTSTART:20261016T173000Z\r
DTEND:20261016T195000Z\r
SUMMARY:Review session [ECON 100]\r
END:VEVENT\r
END:VCALENDAR\r
"""


def test_feed_parsing():
    ds = from_feed(FEED, TZ)
    assert [(d.key, d.course, d.title, d.kind) for d in ds] == [
        ("canvas:901", "CMST& 101", "Weekly Homework 4", "due"),
        ("feed:event-calendar-event-77", "ECON 100", "Review session", "event"),
        ("canvas:902", "ENGL&101", "Lyric Essay Draft", "due"),
    ]
    assert ds[0].due.astimezone(TZ) == datetime(2026, 10, 15, 23, 59, tzinfo=TZ)
    assert ds[2].due == datetime(2026, 10, 18, 23, 59, tzinfo=TZ)
    assert not any(d.trackable for d in ds)


def test_feed_items_text_but_never_call_and_events_never_alert():
    m = Memory(":memory:")
    ds = from_feed(FEED, TZ)
    soon = datetime(2026, 10, 15, 22, 0, tzinfo=TZ)  # 2h before HW 4, ~20h before the event
    assert raise_deadline_alerts(m, ds, soon) == 1
    (alert,) = m.undelivered_alerts()
    assert alert["level"] == "high" and "Weekly Homework 4" in alert["message"]
    assert "check it's done" in alert["message"]


def test_digest_groups_by_day_and_flags_missing():
    ds = from_assignments("CMST", [
        _assignment(1, "HW 3", datetime(2026, 10, 18, 23, 59, tzinfo=TZ)),
        _assignment(2, "Quiz 2", datetime(2026, 10, 16, 9, 0, tzinfo=TZ), state="submitted"),
        _assignment(3, "HW 1", datetime(2026, 10, 4, 23, 59, tzinfo=TZ), missing=True),
        _assignment(4, "Next month", datetime(2026, 11, 20, 9, 0, tzinfo=TZ)),
    ]) + from_modules("ECON100", 1, [{"id": 1, "name": "Module 3 (DUE OCT 18)"}], TZ, NOW.date())
    text = format_digest(sorted(ds, key=lambda d: d.due), NOW)
    assert "MISSING (1)" in text and "HW 1" in text
    assert text.index("Fri Oct 16") < text.index("Sun Oct 18")
    assert "[x] 9:00AM CMST: Quiz 2" in text
    assert "[?] 11:59PM ECON100: Module 3" in text
    assert "Next month" not in text
    assert "2 of 3 still to do" in text
