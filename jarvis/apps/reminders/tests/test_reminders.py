from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from jarvis.contract.api import JobResult, JobRun
from jarvis.core import actions

LA = ZoneInfo("America/Los_Angeles")
CLI = actions.Origin("cli")


def do(reg, action, **args):
    return actions.run(reg, f"reminders__{action}", args, CLI)


def later(**kw):
    return (datetime.now(timezone.utc) + timedelta(**kw)).astimezone(LA).isoformat(timespec="seconds")


def deliver(reg, now):
    app = reg.apps["reminders"]
    return app.module.deliver_due(app.ctx, JobRun(now.astimezone(LA), None, "every", False))


def statuses(reg):
    return [tuple(r) for r in reg.conn.execute("SELECT id, status FROM reminders_items ORDER BY id")]


def test_remind_me_saves_it_wakes_the_job_and_can_be_undone(real_apps):
    woke = []
    real_apps.apps["reminders"].ctx.wake_hook = lambda app, job: woke.append((app, job))
    out = do(real_apps, "remind_me", text="call the bank", at=later(hours=2))
    assert isinstance(out, actions.Ran) and "call the bank" in out.result.text and "#1" in out.result.text
    assert woke == [("reminders", "deliver")]
    assert statuses(real_apps) == [(1, "pending")]
    assert isinstance(actions.undo(real_apps), actions.Ran)
    assert statuses(real_apps) == []
    assert woke == [("reminders", "deliver")] * 2  # the job learns the next time again


@pytest.mark.parametrize("at,fragment", [
    ("2020-01-01T09:00:00-08:00", "already passed"),
    ("not a time", "couldn't read"),
    ("2030-01-01T09:00:00", "no UTC offset"),
])
def test_bad_times_are_refused_in_plain_words(real_apps, at, fragment):
    out = do(real_apps, "remind_me", text="x", at=at)
    assert isinstance(out, actions.Failed) and fragment in out.message
    assert statuses(real_apps) == []


def test_more_than_two_years_ahead_is_refused(real_apps):
    out = do(real_apps, "remind_me", text="x", at=later(days=800))
    assert isinstance(out, actions.Failed) and "more than two years" in out.message
    assert isinstance(do(real_apps, "remind_me", text="x", at=later(days=700)), actions.Ran)


def test_deliver_claims_each_reminder_once_and_returns_the_next_due_time(real_apps):
    now = datetime.now(timezone.utc)
    do(real_apps, "remind_me", text="stretch", at=later(minutes=5))
    do(real_apps, "remind_me", text="call the bank", at=later(hours=3))
    result = deliver(real_apps, now + timedelta(minutes=5, seconds=2))
    assert isinstance(result, JobResult)
    assert result.messages == ("⏰ stretch",) and result.summary == "sent 1"
    assert result.wake_at - (now + timedelta(hours=3)) < timedelta(seconds=2)  # the next one, exactly
    again = deliver(real_apps, now + timedelta(minutes=6))
    assert again.messages == () and again.summary == "sent 0"
    assert statuses(real_apps) == [(1, "sent"), (2, "pending")]


def test_no_wake_when_nothing_is_waiting(real_apps):
    assert deliver(real_apps, datetime.now(timezone.utc)).wake_at is None


def test_a_late_reminder_says_what_it_was_for(real_apps):
    due = datetime(2026, 10, 27, 15, 0, tzinfo=LA)
    real_apps.conn.execute("INSERT INTO reminders_items (due_at, text, created_at) VALUES (?,?,?)",
                           (due.astimezone(timezone.utc).isoformat(), "call the bank", "2026-10-26T00:00:00+00:00"))
    late = deliver(real_apps, due + timedelta(minutes=45))
    assert late.messages == ("⏰ call the bank\n(late: this was for Tue 3:00 PM)",)


def test_ten_minutes_late_or_less_is_on_time(real_apps):
    due = datetime(2026, 10, 27, 15, 0, tzinfo=LA)
    real_apps.conn.execute("INSERT INTO reminders_items (due_at, text, created_at) VALUES (?,?,?)",
                           (due.astimezone(timezone.utc).isoformat(), "stretch", "2026-10-26T00:00:00+00:00"))
    assert deliver(real_apps, due + timedelta(minutes=10)).messages == ("⏰ stretch",)


def test_list_and_cancel_and_undo(real_apps):
    do(real_apps, "remind_me", text="soon", at=later(hours=1))
    do(real_apps, "remind_me", text="far", at=later(days=30))
    listed = do(real_apps, "list_reminders")
    assert "soon" in listed.result.text and "far" not in listed.result.text
    assert len(do(real_apps, "list_reminders", days=60).result.data["reminders"]) == 2
    assert isinstance(do(real_apps, "cancel_reminder", id=1), actions.Ran)
    assert statuses(real_apps)[0] == (1, "cancelled")
    assert "soon" not in do(real_apps, "list_reminders").result.text
    assert isinstance(actions.undo(real_apps), actions.Ran)
    assert statuses(real_apps)[0] == (1, "pending")


def test_cancelling_the_wrong_thing_fails_plainly(real_apps):
    assert "no reminder #9" in do(real_apps, "cancel_reminder", id=9).message
    do(real_apps, "remind_me", text="x", at=later(hours=1))
    do(real_apps, "cancel_reminder", id=1)
    assert "already cancelled" in do(real_apps, "cancel_reminder", id=1).message


def test_the_context_lists_what_is_due_in_a_day_at_most_five(real_apps):
    app = real_apps.apps["reminders"]
    assert app.module.context(app.ctx) == ""
    for i in range(7):
        do(real_apps, "remind_me", text=f"item {i}", at=later(hours=1 + i))
    do(real_apps, "remind_me", text="next week", at=later(days=7))
    block = app.module.context(app.ctx)
    assert block.count("\n- ") == 5 and "item 0" in block and "item 5" not in block and "next week" not in block


def test_the_migration_copies_old_reminders_and_does_not_fire_old_ones(settings):
    from jarvis.core import db
    from jarvis.core.apps import load_apps

    conn = db.connect(settings)
    rows = [
        (1, "2026-01-01T09:00:00-08:00", "weeks old, never sent", None, 0),
        (2, "2026-01-02T09:00:00-08:00", "already sent", "2026-01-02T17:00:00+00:00", 0),
        (3, "2099-01-01T09:00:00-08:00", "far in the future", None, 0),
        (4, "2099-02-01T09:00:00-08:00", "cancelled", None, 1),
    ]
    for rid, due, text, sent, cancelled in rows:
        conn.execute("INSERT INTO reminders (id, due_at, text, created_at, sent_at, cancelled) VALUES (?,?,?,?,?,?)",
                     (rid, due, text, "2025-12-31T00:00:00+00:00", sent, cancelled))
    conn.close()
    reg = load_apps(settings)
    try:
        assert statuses(reg) == [(1, "imported"), (2, "sent"), (3, "pending"), (4, "cancelled")]
        assert deliver(reg, datetime.now(timezone.utc)).messages == ()  # the first run fires nothing old
        assert reg.conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 4  # the old table is left alone
    finally:
        reg.close()


def test_a_timer_is_a_reminder_that_arrives_on_time_and_can_be_undone(real_apps):
    woke = []
    real_apps.apps["reminders"].ctx.wake_hook = lambda app, job: woke.append(job)
    start = datetime.now(timezone.utc)
    out = do(real_apps, "set_timer", minutes=10, label="tea")
    assert isinstance(out, actions.Ran) and out.result.text.startswith("Timer set for 10 min, until ")
    assert woke == ["deliver"]
    (due,) = [datetime.fromisoformat(r[0]) for r in real_apps.conn.execute("SELECT due_at FROM reminders_items")]
    assert timedelta(minutes=9, seconds=58) <= due - start <= timedelta(minutes=10, seconds=2)
    assert deliver(real_apps, start + timedelta(minutes=9)).messages == ()
    assert deliver(real_apps, start + timedelta(minutes=10, seconds=3)).messages == ("⏰ Timer done: tea (10 min)",)
    do(real_apps, "set_timer", minutes=5)
    assert isinstance(actions.undo(real_apps), actions.Ran)
    assert statuses(real_apps) == [(1, "sent")]


def test_timer_limits(real_apps):
    assert isinstance(do(real_apps, "set_timer", minutes=0), actions.Failed)
    assert isinstance(do(real_apps, "set_timer", minutes=1441), actions.Failed)
