"""The scheduler: apps' jobs, run on time inside `jarvis serve`. One asyncio task, one worker thread per run."""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
import time as clock_time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from jarvis.contract.api import ActionError, JobResult, JobRun, Think
from jarvis.contract.timespec import parse_clock
from jarvis.core import db, jobstate
from jarvis.core.apps import Registry
from jarvis.core.jobstate import Job, backoff
from jarvis.core.timeutil import clock_times, next_clock, parse_quiet_hours, quiet_ends, stamp

log = logging.getLogger("jarvis.core.scheduler")

MAX_SLEEP_S = 30
MAX_THREADS = 4
GRACE = timedelta(seconds=90)  # later than this after its time, a run is a catch-up
FAIL_REPORT_AFTER = 3
NET_TTL_S = 30
NET_RETRY = timedelta(minutes=1)
MIN_WAKE = timedelta(seconds=30)  # a handler asking to wake in the past can't spin the job
STUCK_LOG_S = 300

Notify = Callable[[str, str, "datetime | None"], None]
ThinkFn = Callable[[str, str, Think, "datetime | None"], Awaitable[str]]


class Skip(Exception):
    """Raised by the thinking-turn runner when the run should be recorded as skipped (the budget)."""


@dataclass(frozen=True)
class RunOutcome:
    status: str  # ok | failed | skipped | missed
    summary: str = ""
    error: str | None = None


class NetCheck:
    """A cheap 'are we online': a TCP connect to 1.1.1.1:443, remembered for 30 seconds."""

    def __init__(self, host: str = "1.1.1.1", port: int = 443) -> None:
        self.host, self.port, self._at, self._ok = host, port, -1e9, True

    def __call__(self) -> bool:
        if clock_time.monotonic() - self._at > NET_TTL_S:
            try:
                socket.create_connection((self.host, self.port), timeout=2).close()
                self._ok = True
            except OSError:
                self._ok = False
            self._at = clock_time.monotonic()
        return self._ok


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Scheduler:
    def __init__(self, reg: Registry, notify: Notify, think: ThinkFn, *, clock: Callable[[], datetime] = _utc_now,
                 sleep: Callable[[float], Awaitable[None]] | None = None,
                 online: Callable[[], bool] | None = None) -> None:
        self.reg, self.notify, self.think, self.clock = reg, notify, think, clock
        self.online = online or NetCheck()
        self._sleep = sleep or self._default_sleep
        self.tz = reg.settings.tz()
        self.window = parse_quiet_hours(reg.settings.quiet_hours)
        self.jobs: dict[tuple[str, str], Job] = {}
        self._wakes: set[tuple[str, str]] = set()
        self._lock = threading.Lock()  # guards _wakes: ctx.wake is called from handler threads
        self._loop: asyncio.AbstractEventLoop | None = None
        self._event: asyncio.Event | None = None
        self._stopping = False
        self._pruned_at: datetime | None = None
        self.load()

    # -- setup ---------------------------------------------------------------------------------------------------

    def _times(self, app, spec) -> list:
        """The job's clock times; '$name' reads the app's setting, and a bad value falls back to its default."""
        texts = []
        for t in spec.at:
            if t.startswith("$"):
                value = app.ctx.settings.get(t[1:])
                if parse_clock(value) is None:
                    default = app.manifest.settings[t[1:]]["default"]
                    log.warning("%s: setting %s=%r is not a time like 07:00; using %s", app.manifest.name, t[1:],
                                value, default)
                    value = default
                t = value
            texts.append(t)
        return clock_times(texts)

    def load(self) -> None:
        now = self.clock().astimezone(self.tz)
        for name, app in self.reg.apps.items():
            for spec in app.manifest.jobs:
                job = Job(app, spec, self._times(app, spec))
                self.jobs[job.key] = job
                row = jobstate.row_of(self.reg.conn, job.key)
                jobstate.load_counters(self.reg.conn, job)
                if row is not None and row["next_run_at"]:
                    job.trigger_at = datetime.fromisoformat(row["next_run_at"])
                    job.trigger_kind = self._kind_of(job, job.trigger_at)
                elif spec.every:  # never ran: first run right away
                    job.trigger_at, job.trigger_kind = now, "every"
                else:
                    job.trigger_at, job.trigger_kind = self._next_trigger(job, now)
                if row is None:
                    jobstate.save(self.reg.conn, job)

    def _kind_of(self, job: Job, due: datetime) -> str:
        if not job.spec.every or not job.at:
            return "every" if job.spec.every else "at"
        local = due.astimezone(self.tz)
        return "at" if local.second == 0 and local.time().replace(tzinfo=None) in job.at else "every"

    def _next_trigger(self, job: Job, after: datetime) -> tuple[datetime | None, str]:
        found = []
        if job.spec.every:
            found.append((after + timedelta(seconds=job.spec.every), "every"))
        if job.at:
            found.append((next_clock(after.astimezone(self.tz), job.at), "at"))
        return min(found) if found else (None, "every")

    def install_hooks(self) -> None:
        for app in self.reg.apps.values():
            app.ctx.wake_hook = self.wake

    # -- waking --------------------------------------------------------------------------------------------------

    def wake(self, app: str, job: str) -> None:
        """Run this job now. Safe from any thread."""
        with self._lock:
            self._wakes.add((app, job))
        if self._loop is not None and self._event is not None:
            try:
                self._loop.call_soon_threadsafe(self._event.set)
            except RuntimeError:  # the loop is closed: we are shutting down
                pass

    def _apply_wakes(self, now: datetime) -> None:
        with self._lock:
            wakes, self._wakes = self._wakes, set()
        for key in wakes:
            job = self.jobs.get(key)
            if job is not None:
                job.wake_at = min(job.wake_at, now) if job.wake_at else now

    # -- the loop ------------------------------------------------------------------------------------------------

    async def _default_sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._event.wait(), seconds)
        except asyncio.TimeoutError:
            pass
        self._event.clear()

    def _delay(self) -> float:
        dues = [j.due_at() for j in self.jobs.values() if j.due_at()]
        if not dues:
            return MAX_SLEEP_S
        return max(0.05, min(MAX_SLEEP_S, (min(dues) - self.clock()).total_seconds()))

    async def run(self) -> None:
        self._loop, self._event = asyncio.get_running_loop(), asyncio.Event()
        self.install_hooks()
        try:
            while not self._stopping:
                try:
                    await self.tick()
                except Exception:
                    log.exception("the scheduler failed a pass")
                await self._sleep(self._delay())
        finally:
            tasks = [j.task for j in self.jobs.values() if j.task is not None and not j.task.done()]
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def stop(self) -> None:
        self._stopping = True
        if self._loop is not None and self._event is not None:
            self._loop.call_soon_threadsafe(self._event.set)

    async def drain(self) -> None:
        """Wait for the runs in progress (for tests and `jarvis jobs run`)."""
        while True:
            tasks = [j.task for j in self.jobs.values() if j.task is not None and not j.task.done()]
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    def _threads(self) -> int:
        return sum(1 for j in self.jobs.values() if j.running and not j.thinking)

    async def tick(self) -> None:
        """Start every job that is due. Never waits for a run."""
        now = self.clock()
        self._apply_wakes(now)
        await self._network_back(now)
        if self._pruned_at is None or now - self._pruned_at > timedelta(hours=1):
            self._pruned_at = now
            jobstate.prune_runs(self.reg.conn, now)
        for job in self.jobs.values():
            due = job.due_at()
            if due is None or due > now:
                continue
            if job.running:
                self._note_stuck(job, now)
                continue
            if self._threads() >= MAX_THREADS:
                break
            reason = self._start(job, now)
            if reason:
                job.task = asyncio.create_task(self._run(job, reason, now))
                job.task.add_done_callback(lambda _: self._event and self._event.set())  # its wake_at may be sooner

    def _note_stuck(self, job: Job, now: datetime) -> None:
        if job.thread is not None and job.thread.is_alive() and (job.task is None or job.task.done()):
            if not job.stuck_logged and job.last_run_at and (now - job.last_run_at).total_seconds() > STUCK_LOG_S:
                job.stuck_logged = True
                log.warning("%s.%s: the timed-out run is still going, so it can't start again yet", *job.key)

    def _start(self, job: Job, now: datetime) -> str | None:
        """Pick why the job runs now and consume what triggered it. None if the run was missed instead."""
        jobstate.load_counters(self.reg.conn, job)
        candidates = [(t, label) for t, label in ((job.trigger_at, "trigger"), (job.wake_at, "wake"),
                                                  (job.retry_at, "retry")) if t is not None and t <= now]
        first = min(candidates)
        reason = {"wake": "wake", "retry": job.trigger_kind, "trigger": job.trigger_kind}[first[1]]
        if any(label == "trigger" for _, label in candidates):
            late = now - job.trigger_at
            catch_up = timedelta(minutes=self.reg.settings.catch_up_minutes)
            if first[1] == "trigger":
                if job.trigger_kind == "at" and late > catch_up:
                    missed = job.trigger_at.astimezone(self.tz)
                    jobstate.record_run(self.reg.conn, job, now, 0, "missed", "at", f"missed {missed:%a %H:%M}")
                    job.last_status, reason = "missed", None
                elif late > GRACE:
                    reason = "catch-up"
            job.trigger_at, job.trigger_kind = self._next_trigger(job, now)
        if reason is not None:
            if any(label == "wake" for _, label in candidates):
                job.wake_at = None
            if any(label == "retry" for _, label in candidates):
                job.retry_at = None
        jobstate.save(self.reg.conn, job)
        return reason

    async def _network_back(self, now: datetime) -> None:
        waiting = [j for j in self.jobs.values() if j.net_wait]
        if waiting and await asyncio.to_thread(self.online):
            for job in waiting:
                job.net_wait, job.retry_at = False, now + NET_RETRY
                jobstate.save(self.reg.conn, job)

    # -- one run -------------------------------------------------------------------------------------------------

    async def run_manual(self, app: str, name: str, force: bool = False) -> RunOutcome:
        """Run one job now with reason 'manual' (the CLI). `force` ignores quiet hours as well."""
        job = self.jobs.get((app, name))
        if job is None:
            raise KeyError(f"{app}.{name}")
        if job.running:
            return RunOutcome("skipped", "already running")
        jobstate.load_counters(self.reg.conn, job)
        job.task = asyncio.current_task()
        try:
            return await self._run(job, "manual", self.clock(), force=force)
        finally:
            job.task = None

    async def _run(self, job: Job, reason: str, started: datetime, force: bool = False) -> RunOutcome:
        manual = reason == "manual"
        local = started.astimezone(self.tz)
        quiet_end = quiet_ends(local, self.window)
        quiet = quiet_end is not None and not (manual and force)
        source = f"{job.key[0]}.{job.key[1]}"
        t0 = clock_time.monotonic()

        def finish(status: str, summary: str = "", error: str | None = None) -> RunOutcome:
            ms = int((clock_time.monotonic() - t0) * 1000)
            job.last_run_at, job.last_status, job.last_error, job.last_ms = started, status, error, ms
            jobstate.record_run(self.reg.conn, job, started, ms, status, reason, summary, error)
            jobstate.save(self.reg.conn, job, schedule=not manual)
            return RunOutcome(status, summary, error)

        if quiet and job.spec.quiet == "skip":
            return finish("skipped", "quiet hours")
        if "network" in job.spec.needs and not await asyncio.to_thread(self.online):
            job.net_wait = True
            return finish("skipped", "offline")
        not_before = quiet_end if quiet and job.spec.quiet == "hold" else None
        run = JobRun(local, job.last_ok_at.astimezone(self.tz) if job.last_ok_at else None, reason, quiet)
        try:
            result = await self._call(job, run)
            for text in result.messages:
                self.notify(text, source, not_before)
            summary = result.summary
            if result.think is not None:
                job.thinking = True
                try:
                    summary = f"{summary}; {await self.think(*job.key, result.think, not_before)}".strip("; ")
                finally:
                    job.thinking = False
        except Skip as e:
            return finish("skipped", str(e))
        except Exception as e:
            return await self._failed(job, run, e, finish, not_before, source)
        job.failures, job.last_ok_at = 0, started
        if job.reported_at:
            job.reported_at = None
            self.notify(f"{job.spec.title[:1].upper()}{job.spec.title[1:]} is working again.", source, not_before)
        if result.wake_at is not None and not manual:
            wake = result.wake_at if result.wake_at > self.clock() else self.clock() + MIN_WAKE
            job.wake_at = min(job.wake_at, wake) if job.wake_at else wake
        return finish("ok", summary)

    async def _failed(self, job: Job, run: JobRun, error: Exception, finish, not_before, source: str) -> RunOutcome:
        if isinstance(error, asyncio.TimeoutError):
            text = f"timed out after {job.spec.timeout_s:g}s"
        elif isinstance(error, ActionError):
            text = str(error)
        else:
            log.error("%s.%s failed", *job.key, exc_info=error)
            text = f"{type(error).__name__}: {error}"[:200]
        if await asyncio.to_thread(self.online):  # a Mac that just woke up has no network yet: not the job's fault
            job.failures += 1
        if run.reason != "manual":
            job.retry_at = self.clock() + backoff(max(job.failures, 1))
        if job.failures >= FAIL_REPORT_AFTER and not job.reported_at:
            job.reported_at = stamp(self.clock())
            self.notify(f"{job.spec.title[:1].upper()}{job.spec.title[1:]} keeps failing ({text}). "
                        "I'll keep trying and tell you when it works.", source, not_before)
        return finish("failed", error=text)

    async def _call(self, job: Job, run: JobRun) -> JobResult:
        """The handler, in a worker thread with its own database connection, held to the job's timeout."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        settings, app = self.reg.settings, job.app

        def work() -> None:
            conn = db.connect_extra(settings)
            try:
                out = getattr(app.module, job.spec.handler)(app.ctx.with_db(conn), run)
                if not isinstance(out, JobResult):
                    raise TypeError(f"{job.spec.handler} returned {type(out).__name__}, not JobResult")
                outcome: tuple = (out, None)
            except BaseException as e:
                outcome = (None, e)
            finally:
                conn.close()
            try:
                loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(outcome))
            except RuntimeError:  # the loop closed while we ran
                pass

        job.stuck_logged = False
        job.thread = threading.Thread(target=work, name=f"job-{job.key[0]}-{job.key[1]}", daemon=True)
        job.thread.start()
        out, err = await asyncio.wait_for(asyncio.shield(fut), job.spec.timeout_s)
        if err is not None:
            raise err
        return out
