"""What the scheduler remembers about one job: the live state and its row in the `jobs` table."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from jarvis.core.apps import LoadedApp
from jarvis.contract.manifest import JobSpec
from jarvis.core.timeutil import stamp

BACKOFF_S = (60, 120, 300, 600)  # then every 30 minutes
BACKOFF_LONG_S = 1800
RUNS_KEEP = timedelta(days=14)


def backoff(failures: int) -> timedelta:
    return timedelta(seconds=BACKOFF_S[failures - 1] if 1 <= failures <= len(BACKOFF_S) else BACKOFF_LONG_S)


@dataclass
class Job:
    app: LoadedApp
    spec: JobSpec
    at: list[time]  # the spec's clock times with any "$setting" resolved
    trigger_at: datetime | None = None  # the next normal trigger
    trigger_kind: str = "every"  # "every" or "at": which trigger trigger_at came from
    wake_at: datetime | None = None
    retry_at: datetime | None = None
    net_wait: bool = False  # skipped for lack of network; try again soon after it returns
    last_run_at: datetime | None = None
    last_ok_at: datetime | None = None
    last_status: str | None = None
    last_error: str | None = None
    last_ms: int | None = None
    failures: int = 0
    reported_at: str | None = None
    task: object = None  # the asyncio task of a run in progress
    thread: threading.Thread | None = None  # the worker thread; a timed-out one lives on until it returns
    stuck_logged: bool = False
    thinking: bool = False  # waiting on its thinking turn: no thread of its own

    @property
    def key(self) -> tuple[str, str]:
        return self.app.manifest.name, self.spec.name

    @property
    def running(self) -> bool:
        return (self.task is not None and not self.task.done()) or (self.thread is not None and self.thread.is_alive())

    def due_at(self) -> datetime | None:
        times = [t for t in (self.trigger_at, self.wake_at, self.retry_at) if t is not None]
        return min(times) if times else None


def _when(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


def row_of(conn: sqlite3.Connection, key: tuple[str, str]) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs WHERE app=? AND name=?", key).fetchone()


def load_counters(conn: sqlite3.Connection, job: Job) -> None:
    """The row is the truth for what happened (another process, `jarvis jobs run`, may have run the job)."""
    row = row_of(conn, job.key)
    if row is None:
        return
    job.last_run_at, job.last_ok_at = _when(row["last_run_at"]), _when(row["last_ok_at"])
    job.last_status, job.last_error, job.last_ms = row["last_status"], row["last_error"], row["last_ms"]
    job.failures, job.reported_at = row["failures"], row["reported_at"]


def save(conn: sqlite3.Connection, job: Job, schedule: bool = True) -> None:
    """Write the job's row. With schedule=False (a manual run in another process) next_run_at is left alone."""
    due = stamp(job.due_at()) if job.due_at() else None
    conn.execute(
        "INSERT INTO jobs (app, name, next_run_at, last_run_at, last_ok_at, last_status, last_error, last_ms, "
        "failures, reported_at) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(app, name) DO UPDATE SET "
        "next_run_at=CASE WHEN ? THEN excluded.next_run_at ELSE jobs.next_run_at END, "
        "last_run_at=excluded.last_run_at, last_ok_at=excluded.last_ok_at, last_status=excluded.last_status, "
        "last_error=excluded.last_error, last_ms=excluded.last_ms, failures=excluded.failures, "
        "reported_at=excluded.reported_at",
        (*job.key, due, stamp(job.last_run_at) if job.last_run_at else None,
         stamp(job.last_ok_at) if job.last_ok_at else None, job.last_status, job.last_error, job.last_ms,
         job.failures, job.reported_at, schedule),
    )


def record_run(conn: sqlite3.Connection, job: Job, started: datetime, ms: int, status: str, reason: str,
               summary: str | None = None, error: str | None = None) -> None:
    conn.execute(
        "INSERT INTO job_runs (app, name, started_at, ms, status, reason, summary, error) VALUES (?,?,?,?,?,?,?,?)",
        (*job.key, stamp(started), ms, status, reason, summary and summary[:300], error and error[:300]),
    )


def prune_runs(conn: sqlite3.Connection, now: datetime) -> int:
    return conn.execute("DELETE FROM job_runs WHERE started_at < ?", (stamp(now - RUNS_KEEP),)).rowcount
