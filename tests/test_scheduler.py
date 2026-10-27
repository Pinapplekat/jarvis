"""The scheduler with a clock the test moves and no waiting on real time."""

import asyncio
import time as real_time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from jarvis.contract.api import Think
from jarvis.core import jobstate
from jarvis.core.scheduler import Scheduler, Skip

LA = ZoneInfo("America/Los_Angeles")
NOON = datetime(2026, 10, 26, 12, 0, tzinfo=LA)  # a Monday


class Clock:
    def __init__(self, now):
        self.now = now.astimezone(timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


class Harness:
    def __init__(self, reg, now=NOON, quiet="", online=True, think=None, sched_cls=Scheduler):
        reg.settings = replace(reg.settings, quiet_hours=quiet)
        self.reg, self.clock, self.notes, self.net = reg, Clock(now), [], [online]
        self.mod = reg.apps["ticker"].module
        self.thoughts = []

        async def default_think(app, job, think_, not_before):
            self.thoughts.append((app, job, think_, not_before))
            return "replied"

        self.sched = sched_cls(reg, lambda t, s, nb: self.notes.append((t, s, nb)), think or default_think,
                               clock=self.clock, online=lambda: self.net[0])

    def calls(self, job=None):
        return [c for c in self.mod.CALLS if job in (None, c[0])]

    def runs(self, job=None):
        rows = self.reg.conn.execute("SELECT * FROM job_runs ORDER BY id").fetchall()
        return [r for r in rows if job in (None, r["name"])]

    def job(self, name):
        return self.sched.jobs[("ticker", name)]

    async def step(self, **advance):
        self.clock.advance(**advance)
        await self.sched.tick()
        await self.sched.drain()


def go(coro):
    return asyncio.run(coro)


def test_every_jobs_run_at_start_and_then_each_interval(jobs_reg):
    async def t():
        h = Harness(jobs_reg)
        await h.step()
        assert [c[:2] for c in h.calls("beat")] == [("beat", "every")]
        await h.step(minutes=14)
        assert len(h.calls("beat")) == 1
        await h.step(minutes=1)
        assert [c[1] for c in h.calls("beat")] == ["every", "every"]

    go(t())


def test_at_jobs_wait_for_their_time_in_the_timezone_setting(jobs_reg):
    async def t():
        h = Harness(jobs_reg)
        await h.step()
        assert h.calls("daily") == []
        await h.step(hours=9)  # 9:00 PM Los Angeles
        assert [c[:2] for c in h.calls("daily")] == [("daily", "at")]
        await h.step(hours=10)  # 7:00 AM next morning
        assert len(h.calls("daily")) == 2

    go(t())


def test_a_setting_can_name_the_time(jobs_reg):
    async def t():
        h = Harness(jobs_reg)
        assert h.job("named").trigger_at.astimezone(LA) == datetime(2026, 10, 27, 6, 30, tzinfo=LA)

    go(t())


def test_a_bad_time_in_a_setting_falls_back_to_the_default(settings, jobs_reg):
    jobs_reg.apps["ticker"].ctx.settings["at_time"] = "half past six"
    h = Harness(jobs_reg)
    assert h.job("named").trigger_at.astimezone(LA).strftime("%H:%M") == "06:30"


def test_the_first_ever_at_run_does_not_catch_up(jobs_reg):
    async def t():
        h = Harness(jobs_reg, now=datetime(2026, 10, 26, 22, 0, tzinfo=LA))  # after 21:00, no history yet
        await h.step()
        assert h.calls("daily") == [] and h.runs("daily") == []

    go(t())


def test_missed_every_runs_collapse_into_one_catch_up(jobs_reg):
    async def t():
        h = Harness(jobs_reg)
        await h.step()
        await h.step(hours=10)  # forty 15-minute intervals while the Mac slept
        assert [c[1] for c in h.calls("beat")] == ["every", "catch-up"]
        await h.step(minutes=1)
        assert len(h.calls("beat")) == 2

    go(t())


def test_an_at_time_missed_by_a_little_runs_once_as_a_catch_up(jobs_reg):
    async def t():
        h = Harness(jobs_reg)
        await h.step()
        await h.step(hours=10, minutes=30)  # 10:30 PM: the 9:00 PM run is 90 minutes late
        assert [c[1] for c in h.calls("daily")] == ["catch-up"]
        assert h.job("daily").trigger_at.astimezone(LA) == datetime(2026, 10, 27, 7, 0, tzinfo=LA)

    go(t())


def test_an_at_time_missed_by_too_much_is_recorded_and_skipped(jobs_reg):
    async def t():
        h = Harness(jobs_reg)
        await h.step()
        await h.step(hours=12)  # midnight: three hours past 9:00 PM
        assert h.calls("daily") == []
        missed = h.runs("daily")
        assert [(r["status"], r["reason"]) for r in missed] == [("missed", "at")] and "Mon 21:00" in missed[0]["summary"]
        assert h.job("daily").last_status == "missed"

    go(t())


def test_catch_up_minutes_is_a_setting(jobs_reg):
    async def t():
        jobs_reg.settings = replace(jobs_reg.settings, catch_up_minutes=30)
        h = Harness(jobs_reg)
        await h.step()
        await h.step(hours=9, minutes=45)
        assert h.calls("daily") == [] and h.runs("daily")[0]["status"] == "missed"

    go(t())


def test_a_wake_at_from_the_handler_runs_the_job_early(jobs_reg):
    async def t():
        h = Harness(jobs_reg)
        h.mod.CONTROL["beat"] = {"wake_at": NOON + timedelta(minutes=2)}
        await h.step()
        assert len(h.calls("beat")) == 1
        h.mod.CONTROL["beat"] = {}
        await h.step(minutes=1)
        assert len(h.calls("beat")) == 1
        await h.step(minutes=1, seconds=1)
        assert [c[1] for c in h.calls("beat")] == ["every", "wake"]
        assert h.job("beat").wake_at is None

    go(t())


def test_a_wake_at_in_the_past_cannot_spin_the_job(jobs_reg):
    async def t():
        h = Harness(jobs_reg)
        h.mod.CONTROL["beat"] = {"wake_at": NOON - timedelta(hours=1)}
        await h.step()
        await h.step(seconds=5)
        assert len(h.calls("beat")) == 1
        assert h.job("beat").wake_at == h.clock() + timedelta(seconds=25)  # 30 s after the run

    go(t())


def test_ctx_wake_runs_a_job_now_from_any_thread(jobs_reg):
    async def t():
        h = Harness(jobs_reg)
        h.sched.install_hooks()
        await h.step()
        await h.step(minutes=3)
        assert len(h.calls("beat")) == 1
        await asyncio.to_thread(jobs_reg.apps["ticker"].ctx.wake, "beat")  # as an action handler would
        await h.step()
        assert [c[1] for c in h.calls("beat")] == ["every", "wake"]
        jobs_reg.apps["ticker"].ctx.wake("no_such_job")  # ignored
        await h.step()

    go(t())


def test_ctx_wake_does_nothing_without_a_scheduler(jobs_reg):
    jobs_reg.apps["ticker"].ctx.wake("beat")  # no hook installed: no error


def test_a_wake_during_a_run_is_not_lost(jobs_reg):
    async def t():
        h = Harness(jobs_reg)
        h.sched.install_hooks()
        h.mod.CONTROL["beat"] = {"sleep": 0.2}
        await h.sched.tick()
        h.sched.wake("ticker", "beat")
        await h.sched.tick()  # still running: nothing starts
        await h.sched.drain()
        assert len(h.calls("beat")) == 1
        h.mod.CONTROL["beat"] = {}
        await h.sched.tick()
        await h.sched.drain()
        assert [c[1] for c in h.calls("beat")] == ["every", "wake"]

    go(t())


def test_one_run_of_a_job_at_a_time_and_a_timed_out_run_blocks_the_next(jobs_reg):
    async def t():
        h = Harness(jobs_reg)
        beat = h.job("beat")
        beat.spec = replace(beat.spec, timeout_s=0.1)
        h.mod.CONTROL["beat"] = {"sleep": 0.6}
        await h.sched.tick()
        await h.sched.tick()
        await asyncio.sleep(0.05)
        assert len(h.calls("beat")) == 1  # a second tick doesn't start a second run
        await h.sched.drain()
        run = h.runs("beat")[0]
        assert run["status"] == "failed" and "timed out after 0.1s" in run["error"]
        await h.step(minutes=2)  # the retry is due, but the thread is still going
        assert len(h.calls("beat")) == 1
        real_time.sleep(0.7)  # now the thread has actually ended
        await h.step(minutes=1)
        assert len(h.calls("beat")) == 2

    go(t())


def test_handlers_run_in_worker_threads_each_with_its_own_connection(jobs_reg):
    async def t():
        h = Harness(jobs_reg)
        h.mod.CONTROL["beat"] = {"db": True}
        await h.step()
        assert h.calls("beat")[0][3].startswith("job-ticker-beat")
        assert h.mod.CONTROL["conns"][0] not in (id(jobs_reg.conn), id(jobs_reg.apps["ticker"].ctx.db))

    go(t())


def test_at_most_four_job_threads_at_once(jobs_reg):
    async def t():
        h = Harness(jobs_reg)
        for name in ("beat", "silent", "needy", "thinker"):
            h.mod.CONTROL[name] = {"sleep": 0.3}
        h.mod.CONTROL["named"] = {}
        for name in ("beat", "silent", "needy", "thinker"):
            h.job(name).trigger_at = NOON
        h.job("named").trigger_at = NOON
        await h.sched.tick()
        await asyncio.sleep(0.1)
        started = {c[0] for c in h.calls()}
        assert len(started) == 4 and "named" not in started
        await h.sched.drain()
        await h.sched.tick()
        await h.sched.drain()
        assert "named" in {c[0] for c in h.calls()}

    go(t())


def test_offline_skips_without_a_failure_or_a_message(jobs_reg):
    async def t():
        h = Harness(jobs_reg, online=False)
        await h.step()
        run = h.runs("needy")[0]
        assert (run["status"], run["summary"], run["error"]) == ("skipped", "offline", None)
        assert h.calls("needy") == [] and h.notes == []
        assert h.job("needy").failures == 0
        await h.step(minutes=5)
        assert h.calls("needy") == [] and h.notes == []  # still offline: nothing, quietly
        h.net[0] = True
        await h.step(seconds=1)  # the network is back: retry in a minute, not at the next hourly trigger
        assert h.calls("needy") == []
        await h.step(seconds=61)
        assert len(h.calls("needy")) == 1 and h.notes == []

    go(t())


def test_a_failure_while_offline_does_not_count(jobs_reg):
    async def t():
        h = Harness(jobs_reg, online=False)
        h.mod.CONTROL["beat"] = {"fail": "boom"}  # beat doesn't need the network, but the Mac just woke up
        for _ in range(5):
            await h.step(minutes=3)
        assert h.job("beat").failures == 0 and h.notes == []
        assert {r["status"] for r in h.runs("beat")} == {"failed"}

    go(t())


def test_three_failures_online_send_one_message_and_recovery_one(jobs_reg):
    async def t():
        h = Harness(jobs_reg)
        h.mod.CONTROL["needy"] = {"fail": "401 Unauthorized"}
        await h.step()
        await h.step(minutes=1, seconds=1)  # retries after 1, 2, 5, 10 minutes
        assert h.notes == [] and h.job("needy").failures == 2
        await h.step(minutes=2, seconds=1)
        assert [n[0] for n in h.notes] == [
            "Canvas sync keeps failing (401 Unauthorized). I'll keep trying and tell you when it works."]
        for minutes in (5, 10, 30, 30):
            await h.step(minutes=minutes, seconds=1)
        assert len(h.notes) == 1 and h.job("needy").failures == 7
        h.mod.CONTROL["needy"] = {}
        await h.step(minutes=30, seconds=1)
        assert [n[0] for n in h.notes][1:] == ["Canvas sync is working again."]
        assert h.job("needy").failures == 0 and h.job("needy").reported_at is None
        await h.step(hours=1, seconds=1)
        assert len(h.notes) == 2  # nothing more

    go(t())


def test_a_crash_is_a_failure_too(jobs_reg):
    async def t():
        h = Harness(jobs_reg)
        h.mod.CONTROL["beat"] = {"fail": "bad data", "action_error": False}
        await h.step()
        assert h.runs("beat")[0]["error"] == "RuntimeError: bad data"

    go(t())


def test_a_handler_must_return_a_job_result(jobs_reg):
    async def t():
        h = Harness(jobs_reg)
        h.mod.beat = lambda ctx, run: "oops"
        await h.step()
        assert "not JobResult" in h.runs("beat")[0]["error"]

    go(t())


def test_state_survives_a_restart_without_a_repeat(jobs_reg):
    async def t():
        first = Harness(jobs_reg)
        first.mod.CONTROL["beat"] = {"wake_at": NOON + timedelta(minutes=4)}
        await first.step()
        due = first.job("beat").due_at()
        first.mod.CONTROL["beat"] = {}
        second = Harness(jobs_reg, now=NOON + timedelta(minutes=1))  # a new process, a minute later
        second.mod.CALLS.clear()
        assert second.job("beat").due_at().replace(microsecond=0) == due.replace(microsecond=0)
        assert second.job("beat").last_status == "ok" and second.job("beat").last_run_at is not None
        await second.step()
        assert second.calls("beat") == []  # no repeat
        await second.step(minutes=3, seconds=1)
        assert len(second.calls("beat")) == 1  # and the wake isn't forgotten

    go(t())


def test_a_restart_after_the_due_time_runs_the_job_once_late(jobs_reg):
    async def t():
        first = Harness(jobs_reg)
        await first.step()
        second = Harness(jobs_reg, now=NOON + timedelta(hours=2))
        second.mod.CALLS.clear()
        await second.step()
        await second.step(seconds=1)
        assert [c[1] for c in second.calls("beat")] == ["catch-up"]

    go(t())


def test_the_jobs_row_records_the_last_run(jobs_reg):
    async def t():
        h = Harness(jobs_reg)
        h.mod.CONTROL["beat"] = {"summary": "sent 2"}
        await h.step()
        row = jobs_reg.conn.execute("SELECT * FROM jobs WHERE name='beat'").fetchone()
        assert (row["last_status"], row["failures"], row["last_error"]) == ("ok", 0, None)
        assert row["last_ok_at"] and row["next_run_at"] and row["last_ms"] is not None
        assert h.runs("beat")[0]["summary"] == "sent 2"

    go(t())


def test_job_runs_older_than_14_days_are_pruned(jobs_reg):
    async def t():
        h = Harness(jobs_reg)
        job = h.job("beat")
        for days in (20, 15, 13, 1):
            jobstate.record_run(jobs_reg.conn, job, h.clock() - timedelta(days=days), 1, "ok", "every", f"{days}d")
        await h.step()
        left = sorted(r["summary"] for r in h.runs("beat") if r["summary"].endswith("d"))
        assert left == ["13d", "1d"]

    go(t())


def test_quiet_hours_ignore_runs_and_sends_skip_does_not_run(jobs_reg):
    async def t():
        h = Harness(jobs_reg, now=datetime(2026, 10, 26, 23, 30, tzinfo=LA), quiet="23-7")  # crosses midnight
        h.mod.CONTROL["beat"] = {"messages": ["hello"]}
        await h.step()
        assert h.calls("beat")[0][2] is True  # JobRun.quiet
        assert h.notes == [("hello", "ticker.beat", None)]  # ignore: sent right away
        assert h.calls("silent") == []
        assert [(r["status"], r["summary"]) for r in h.runs("silent")] == [("skipped", "quiet hours")]

    go(t())


def test_quiet_hours_hold_runs_but_the_messages_wait_for_the_end(jobs_reg):
    async def t():
        h = Harness(jobs_reg, now=datetime(2026, 10, 26, 20, 59, tzinfo=LA), quiet="20-22")
        h.mod.CONTROL["daily"] = {"messages": ["good evening"]}
        await h.step(minutes=1, seconds=1)
        assert len(h.calls("daily")) == 1
        (text, source, not_before), = h.notes
        assert (text, source) == ("good evening", "ticker.daily")
        assert not_before == datetime(2026, 10, 26, 22, 0, tzinfo=LA)

    go(t())


def test_outside_quiet_hours_nothing_is_held(jobs_reg):
    async def t():
        h = Harness(jobs_reg, quiet="23-7")
        h.mod.CONTROL["beat"] = {"messages": ["hello"]}
        await h.step()
        assert h.notes == [("hello", "ticker.beat", None)] and h.calls("beat")[0][2] is False

    go(t())


def test_a_thinking_request_goes_to_the_host_with_the_quiet_hold(jobs_reg):
    async def t():
        h = Harness(jobs_reg, now=datetime(2026, 10, 26, 23, 30, tzinfo=LA), quiet="23-7")
        h.mod.CONTROL["thinker"] = {"think": Think("about it"), "summary": "asked"}
        await h.step()
        (app, job, think, not_before), = h.thoughts
        assert (app, job, think.prompt) == ("ticker", "thinker", "about it")
        assert not_before == datetime(2026, 10, 27, 7, 0, tzinfo=LA)
        assert h.runs("thinker")[0]["summary"] == "asked; replied"

    go(t())


def test_a_skipped_thinking_turn_is_recorded_as_skipped(jobs_reg):
    async def t():
        async def over_budget(app, job, think, not_before):
            raise Skip("budget")

        h = Harness(jobs_reg, think=over_budget)
        h.mod.CONTROL["thinker"] = {"think": Think("x")}
        await h.step()
        run = h.runs("thinker")[0]
        assert (run["status"], run["summary"]) == ("skipped", "budget")
        assert h.job("thinker").failures == 0 and h.notes == []

    go(t())


def test_a_failing_thinking_turn_is_a_failed_run(jobs_reg):
    async def t():
        async def broken(app, job, think, not_before):
            raise RuntimeError("brain down")

        h = Harness(jobs_reg, think=broken)
        h.mod.CONTROL["thinker"] = {"think": Think("x")}
        await h.step()
        assert h.runs("thinker")[0]["status"] == "failed"

    go(t())


def test_run_manual_ignores_the_trigger_and_leaves_the_schedule_alone(jobs_reg):
    async def t():
        h = Harness(jobs_reg, now=datetime(2026, 10, 26, 23, 30, tzinfo=LA), quiet="23-7")
        due = h.job("daily").due_at()
        h.mod.CONTROL["daily"] = {"messages": ["hi"], "summary": "did it"}
        out = await h.sched.run_manual("ticker", "daily", force=True)
        assert (out.status, out.summary) == ("ok", "did it")
        assert h.calls("daily")[0][1:3] == ("manual", False) and h.notes == [("hi", "ticker.daily", None)]
        assert h.job("daily").due_at() == due
        assert jobs_reg.conn.execute("SELECT next_run_at FROM jobs WHERE name='daily'").fetchone()[0] == jobstate.stamp(due)

    go(t())


def test_run_manual_respects_quiet_hours_unless_forced(jobs_reg):
    async def t():
        h = Harness(jobs_reg, now=datetime(2026, 10, 26, 23, 30, tzinfo=LA), quiet="23-7")
        out = await h.sched.run_manual("ticker", "silent")
        assert (out.status, out.summary) == ("skipped", "quiet hours") and h.calls("silent") == []
        out = await h.sched.run_manual("ticker", "silent", force=True)
        assert out.status == "ok" and len(h.calls("silent")) == 1

    go(t())


def test_manual_failures_count_toward_the_report(jobs_reg):
    async def t():
        h = Harness(jobs_reg)
        h.mod.CONTROL["needy"] = {"fail": "401 Unauthorized"}
        for _ in range(3):
            await h.sched.run_manual("ticker", "needy")
        assert len(h.notes) == 1 and h.notes[0][0].startswith("Canvas sync keeps failing (401")
        h.mod.CONTROL["needy"] = {}
        await h.sched.run_manual("ticker", "needy")
        assert h.notes[-1][0] == "Canvas sync is working again."

    go(t())


def test_two_processes_share_the_failure_count_through_the_jobs_row(jobs_reg):
    async def t():
        serve = Harness(jobs_reg)
        h = Harness(jobs_reg)  # `jarvis jobs run` in another process
        h.mod.CONTROL["needy"] = {"fail": "401 Unauthorized"}
        await h.sched.run_manual("ticker", "needy")
        await h.sched.run_manual("ticker", "needy")
        serve.mod.CONTROL["needy"] = {"fail": "401 Unauthorized"}
        await serve.step()  # the third failure, seen by the other scheduler
        assert len(serve.notes) == 1

    go(t())


def test_an_unknown_job_is_a_key_error(jobs_reg):
    async def t():
        with pytest.raises(KeyError):
            await Harness(jobs_reg).sched.run_manual("ticker", "nope")

    go(t())
