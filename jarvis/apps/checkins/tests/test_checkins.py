from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from jarvis.contract.api import JobRun, Think

LA = ZoneInfo("America/Los_Angeles")
MORNING = datetime(2026, 10, 26, 7, 0, tzinfo=LA)
EVENING = datetime(2026, 10, 26, 21, 0, tzinfo=LA)


def run_job(reg, name, now, reason="at", **settings):
    app = reg.apps["checkins"]
    app.ctx.settings.update(settings)
    return getattr(app.module, name)(app.ctx, JobRun(now, None, reason, False))


def sent(reg):
    return [tuple(r) for r in reg.conn.execute("SELECT day, kind FROM checkins_sent ORDER BY day, kind")]


def seed(reg):
    at = lambda dt: dt.astimezone(timezone.utc).isoformat()
    reg.conn.execute(
        "INSERT INTO school_deadlines (key, course, title, due, status, trackable, kind, hand_done, source, updated_at) "
        "VALUES ('canvas:1','ENGL&101','Lyric Essay Draft',?,'unsubmitted',1,'due',0,'canvas','x'),"
        "('canvas:2','ECON 100','Quiz Three',?,'submitted',1,'due',0,'canvas','x'),"
        "('canvas:3','MATH 100','Far Away',?,'unsubmitted',1,'due',0,'canvas','x')",
        (at(MORNING + timedelta(hours=10)), at(MORNING + timedelta(hours=11)), at(MORNING + timedelta(days=9))))
    reg.conn.execute("INSERT INTO reminders_items (due_at, text, created_at) VALUES (?,?,?)",
                     (at(MORNING + timedelta(hours=8)), "call the bank", "x"))
    reg.conn.execute("INSERT INTO journal_entries (day, entry, open_loops, created_at) VALUES "
                     "('2026-10-25','Elijah worked on the essay.','[\"email the professor\"]','x')")


def test_the_morning_brief_asks_for_a_thinking_turn_with_the_days_data(real_apps):
    seed(real_apps)
    result = run_job(real_apps, "morning", MORNING)
    think = result.think
    assert isinstance(think, Think) and think.deliver and think.untrusted  # it quotes assignment titles
    assert "MORNING BRIEF" in think.prompt and "Discord" in think.prompt and "JSON" not in think.prompt
    assert "Telegram" not in think.prompt
    assert "ENGL&101: Lyric Essay Draft" in think.prompt and "Quiz Three" not in think.prompt  # done is left out
    assert "Far Away" not in think.prompt and "call the bank" in think.prompt
    assert "Elijah worked on the essay." in think.prompt and "email the professor" in think.prompt
    assert sent(real_apps) == [("2026-10-26", "morning")]


def test_the_evening_check_in_has_its_own_instructions(real_apps):
    seed(real_apps)
    result = run_job(real_apps, "evening", EVENING)
    assert "EVENING CHECK-IN" in result.think.prompt and "how the day went" in result.think.prompt
    assert sent(real_apps) == [("2026-10-26", "evening")]


def test_each_goes_out_once_a_day(real_apps):
    assert run_job(real_apps, "morning", MORNING).think is not None
    again = run_job(real_apps, "morning", MORNING + timedelta(minutes=5), reason="wake")
    assert again.think is None and again.summary == "already sent today"
    assert run_job(real_apps, "evening", EVENING).think is not None  # the evening is a different kind
    assert run_job(real_apps, "morning", MORNING + timedelta(days=1)).think is not None  # and tomorrow another


def test_a_manual_run_always_goes_ahead_and_leaves_the_day_alone(real_apps):
    assert run_job(real_apps, "morning", MORNING, reason="manual").think is not None
    assert run_job(real_apps, "morning", MORNING, reason="manual").think is not None
    assert sent(real_apps) == []
    assert run_job(real_apps, "morning", MORNING).think is not None  # the real one is still to come


def test_a_catch_up_after_sleep_may_still_send_the_morning_brief_until_morning_latest(real_apps):
    ok = run_job(real_apps, "morning", MORNING.replace(hour=9, minute=30), reason="catch-up")
    assert ok.think is not None
    late = run_job(real_apps, "morning", (MORNING + timedelta(days=1)).replace(hour=10, minute=1), reason="catch-up")
    assert late.think is None and late.summary == "too late for this morning"
    assert sent(real_apps) == [("2026-10-26", "morning")]  # a morning that was too late isn't marked sent


def test_morning_latest_is_a_setting(real_apps):
    result = run_job(real_apps, "morning", MORNING.replace(hour=11), reason="catch-up", morning_latest="12:00")
    assert result.think is not None


def test_the_evening_check_in_has_no_cut_off(real_apps):
    assert run_job(real_apps, "evening", EVENING.replace(hour=23, minute=30), reason="catch-up").think is not None


def test_either_can_be_turned_off(real_apps):
    assert run_job(real_apps, "morning", MORNING, morning_on=False).summary == "turned off"
    assert run_job(real_apps, "evening", EVENING, evening_on=False).think is None
    assert sent(real_apps) == []


def test_missing_apps_show_as_not_available_instead_of_failing(real_apps):
    real_apps.conn.execute("ALTER TABLE school_deadlines RENAME TO school_deadlines_away")
    prompt = run_job(real_apps, "morning", MORNING).think.prompt
    assert "Deadlines (next 2 days): (not available)" in prompt


def test_the_job_times_come_from_settings(real_apps):
    from jarvis.core.scheduler import Scheduler

    app = real_apps.apps["checkins"]
    app.ctx.settings["morning"], app.ctx.settings["evening"] = "06:15", "20:45"
    sched = Scheduler(real_apps, lambda *a: None, None, clock=lambda: datetime(2026, 10, 26, 5, 0, tzinfo=LA))
    due = {k[1]: j.trigger_at.astimezone(LA).strftime("%H:%M") for k, j in sched.jobs.items() if k[0] == "checkins"}
    assert due == {"morning": "06:15", "evening": "20:45"}
    assert [j.spec.quiet for k, j in sched.jobs.items() if k[0] == "checkins"] == ["skip", "skip"]
