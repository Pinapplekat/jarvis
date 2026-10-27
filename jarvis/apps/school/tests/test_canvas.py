"""The Canvas parsers, ported from the old canvas.py tests. No network."""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from jarvis.apps.school.canvas import (
    Deadline, course_name, format_digest, from_assignments, from_feed, from_modules, merge, same_work, spoken_course,
)

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
        _assignment(6, "Reviewed", NOW + timedelta(days=2), state="pending_review"),
    ])
    assert [(d.title, d.status, d.trackable) for d in ds] == [
        ("HW 3", "unsubmitted", True),
        ("HW 2", "missing", True),
        ("Quiz", "graded", True),
        ("Contract", "unknown", False),
        ("Reviewed", "submitted", True),
    ]
    assert [d.done for d in ds] == [False, False, True, False, True]


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
    assert ds[0].url.startswith("https://canvas.seattlecolleges.edu/calendar")
    assert not any(d.trackable for d in ds)


def test_feed_handles_folded_lines_escapes_and_floating_times():
    ics = (b"BEGIN:VEVENT\r\nUID:event-assignment-5\r\nDTSTART:20261020T180000\r\n"
           b"SUMMARY:Read chapters 1\\, 2\\; and\r\n  3 [PSYC 100]\r\nEND:VEVENT\r\n"
           b"BEGIN:VEVENT\r\nUID:x\r\nDTSTART;TZID=America/New_York:20261020T180000\r\nSUMMARY:Call\r\nEND:VEVENT\r\n")
    b, a = from_feed(ics, TZ)  # sorted by due: 6 PM in New York is earlier than 6 PM in Los Angeles
    assert a.title == "Read chapters 1, 2; and 3" and a.due == datetime(2026, 10, 20, 18, 0, tzinfo=TZ)
    assert b.course == "Canvas" and b.due == datetime(2026, 10, 20, 18, 0, tzinfo=ZoneInfo("America/New_York"))


def test_an_empty_or_odd_feed_gives_nothing():
    assert from_feed(b"", TZ) == []
    assert from_feed(b"BEGIN:VEVENT\r\nSUMMARY:no start\r\nEND:VEVENT\r\n", TZ) == []


def test_digest_groups_by_day_flags_missing_and_ends_with_keys():
    ds = from_assignments("CMST", [
        _assignment(1, "HW 3", datetime(2026, 10, 18, 23, 59, tzinfo=TZ)),
        _assignment(2, "Quiz 2", datetime(2026, 10, 16, 9, 0, tzinfo=TZ), state="submitted"),
        _assignment(3, "HW 1", datetime(2026, 10, 4, 23, 59, tzinfo=TZ), missing=True),
        _assignment(4, "Next month", datetime(2026, 11, 20, 9, 0, tzinfo=TZ)),
    ]) + from_modules("ECON100", 1, [{"id": 1, "name": "Module 3 (DUE OCT 18)"}], TZ, NOW.date())
    text = format_digest(sorted(ds, key=lambda d: d.due), NOW)
    assert "Missing (1)" in text and "HW 1" in text
    assert text.index("Fri Oct 16") < text.index("Sun Oct 18")
    assert "9:00 AM CMST: Quiz 2 (done) [canvas:2]" in text
    assert "11:59 PM Econ: Module 3 (Canvas can't tell if it's done) [module:1:1]" in text
    assert "Next month" not in text
    assert "2 of 3 still to do" in text


def test_digest_with_nothing_due():
    assert "Nothing due." in format_digest([], NOW) and "0 of 0 still to do" in format_digest([], NOW)


def test_events_are_listed_but_not_counted_as_deadlines():
    ds = from_feed(FEED, TZ)
    text = format_digest(ds, NOW)
    assert "Review session (an event, not a deadline)" in text and "2 of 2 still to do" in text


def test_course_names_agree_and_sound_like_him():
    assert course_name("English", {"ENGL&101", "ECON 100"}) == "ENGL&101"
    assert course_name("ENGL&101", {"ENGL&101"}) == "ENGL&101"
    assert course_name("Math", {"ENGL&101"}) == "Math"
    assert spoken_course("ENGL&101") == "English" and spoken_course("CMST& 101") == "Communication"
    assert spoken_course("WEIRD 9") == "WEIRD 9" and spoken_course("ENGL&101", {"ENGL&101": "Writing"}) == "Writing"


def _d(key, course, title, due, status="unknown"):
    return Deadline(key, course, title, due, None, status, False)


def test_same_work_listed_twice_is_one_assignment():
    due = datetime(2026, 10, 18, 23, 59, tzinfo=TZ)
    canvas = _d("canvas:1", "ENGL&101", "Lyric Essay Draft", due)
    assert same_work(canvas, _d("manual:x", "ENGL&101", "Lyric Essay Draft (rough)", due + timedelta(days=1)))
    assert not same_work(canvas, _d("manual:y", "ENGL&101", "Lyric Essay Draft", due + timedelta(days=9)))
    assert not same_work(canvas, _d("manual:z", "ECON 100", "Lyric Essay Draft", due))
    assert not same_work(_d("a", "X", "HW 1", due), _d("b", "X", "HW 1", due))  # too short to be sure


def test_merge_drops_a_hand_made_twin_and_keeps_its_done():
    due = datetime(2026, 10, 18, 23, 59, tzinfo=TZ)
    canvas = [_d("canvas:1", "ENGL&101", "Lyric Essay Draft", due), _d("canvas:2", "ECON 100", "Quiz Three", due)]
    hand = [_d("manual:a", "English", "Lyric Essay Draft (rough)", due, status="submitted"),
            _d("manual:b", "Math", "Worksheet seven", due + timedelta(days=1))]
    merged = merge(canvas, hand)
    assert [d.key for d in merged] == ["canvas:1", "canvas:2", "manual:b"]
    assert merged[0].done and not merged[1].done
    assert merge(canvas, [_d("canvas:1", "ENGL&101", "Changed title", due)])[0].title == "Changed title"
