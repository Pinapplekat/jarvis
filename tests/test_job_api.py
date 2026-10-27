"""The job additions to the app contract: AppContext.now/tz/wake/with_db and the conversation readers."""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from jarvis.contract.api import AppContext, JobResult, JobRun, Think

LA = ZoneInfo("America/Los_Angeles")


def ctx(db=None, tz=None):
    return AppContext("demo", {}, {}, db, Path("."), tz)


def test_now_is_aware_and_in_the_given_zone():
    assert ctx(tz=LA).now().tzinfo is LA
    assert ctx().now().utcoffset() == timedelta(0)  # a device node with no timezone setting: UTC


def test_wake_calls_the_hook_with_the_app_name_and_does_nothing_without_one():
    c, calls = ctx(), []
    c.wake("deliver")
    c.wake_hook = lambda app, job: calls.append((app, job))
    c.wake("deliver")
    assert calls == [("demo", "deliver")]


def test_with_db_is_the_same_context_on_another_connection():
    a, b = sqlite3.connect(":memory:"), sqlite3.connect(":memory:")
    c = ctx(a, LA)
    c.wake_hook = lambda *x: None
    other = c.with_db(b)
    assert other.db is b and c.db is a
    assert (other.name, other.tz, other.settings, other.wake_hook) == (c.name, LA, c.settings, c.wake_hook)


def test_job_values_are_plain_data():
    run = JobRun(datetime.now(LA), None, "every", False)
    assert JobResult("sent 2").messages == () and JobResult("x").think is None and JobResult("x").wake_at is None
    t = Think("hello")
    assert (t.deliver, t.untrusted) == (True, False) and run.reason == "every"


def test_conversation_and_action_readers(real_apps):
    c = real_apps.apps["memory"].ctx
    day = datetime(2026, 10, 25, tzinfo=LA)
    stamp = lambda d: d.astimezone(timezone.utc).isoformat(timespec="seconds")
    real_apps.conn.execute("INSERT INTO chat_log (at, role, text) VALUES (?,?,?)", (stamp(day + timedelta(hours=9)), "user", "hi"))
    real_apps.conn.execute("INSERT INTO chat_log (at, role, text) VALUES (?,?,?)",
                           (stamp(day + timedelta(hours=9, minutes=1)), "assistant", "hello"))
    real_apps.conn.execute("INSERT INTO action_log (time, tool, effect, status, args, result) VALUES (?,?,?,?,?,?)",
                           (stamp(day + timedelta(hours=10)), "memory__recall", "read", "ran", "{}", "No facts"))
    got = c.chat_between(day, day + timedelta(days=1))
    assert [(w.hour, who, text) for w, who, text in got] == [(9, "Elijah", "hi"), (9, "Falak", "hello")]
    assert got[0][0].tzinfo is c.tz
    (when, tool, status, result), = c.actions_between(day, day + timedelta(days=1))
    assert (when.hour, tool, status, result) == (10, "memory__recall", "ran", "No facts")
    assert c.chat_between(day + timedelta(days=1), day + timedelta(days=2)) == []
