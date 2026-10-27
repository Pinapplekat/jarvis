"""The job commands: jobs, jobs run <app>.<job>, outbox."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone

from jarvis.core import outbox
from jarvis.core.apps import load_apps
from jarvis.core.brain import Brain
from jarvis.core.settings import Settings
from jarvis.core.timeutil import ago
from jarvis.host import Host


def _trigger(job, settings: dict) -> str:
    parts = []
    if job.every:
        parts.append(f"every {job.every // 3600}h" if job.every % 3600 == 0 else
                     f"every {job.every // 60}m" if job.every % 60 == 0 else f"every {job.every}s")
    if job.at:
        parts.append("at " + ", ".join(str(settings.get(t[1:], t)) if t.startswith("$") else t for t in job.at))
    return " + ".join(parts)


def _when(text: str | None, now: datetime) -> str:
    if not text:
        return "-"
    when = datetime.fromisoformat(text)
    if when <= now:
        return ago(text, now)
    minutes = round((when - now).total_seconds() / 60)
    return f"in {minutes} min" if minutes < 90 else f"in {round(minutes / 60)} h"


def list_jobs(settings: Settings) -> int:
    reg = load_apps(settings)
    try:
        now = datetime.now(timezone.utc)
        rows = {(r["app"], r["name"]): r for r in reg.conn.execute("SELECT * FROM jobs")}
        for app in reg.apps.values():
            for job in app.manifest.jobs:
                r = rows.get((app.manifest.name, job.name))
                last = "never run"
                if r and r["last_run_at"]:
                    last = f"{r['last_status']} {ago(r['last_run_at'], now)} ({r['last_ms']} ms)"
                    if r["last_error"]:
                        last += f" {r['last_error']}"
                print(f"{app.manifest.name}.{job.name}  {_trigger(job, app.ctx.settings)}  last: {last}  "
                      f"next: {_when(r['next_run_at'] if r else None, now)}  failures: {r['failures'] if r else 0}")
        return 0
    finally:
        reg.close()


def show_outbox(settings: Settings, n: int = 20) -> int:
    reg = load_apps(settings)
    try:
        for r in outbox.recent(reg.conn, n):
            state = f"sent {r['sent_at']}" if r["sent_at"] else (
                f"waiting until {r['not_before']}" if r["not_before"] else "waiting")
            tries = f", {r['attempts']} failed tries ({r['last_error']})" if r["attempts"] and not r["sent_at"] else ""
            print(f"#{r['id']} {r['created_at']} {r['source']} [{state}{tries}]\n  {r['text']}")
        return 0
    finally:
        reg.close()


def run_job(settings: Settings, target: str, force: bool, brain: Brain) -> int:
    """Run one job in this process, the way the scheduler would. Its messages go to the outbox."""
    app, _, name = target.partition(".")
    reg = load_apps(settings)
    host = Host(reg, brain)
    try:
        if (app, name) not in host.scheduler.jobs:
            known = ", ".join(f"{a}.{n}" for a, n in host.scheduler.jobs) or "none"
            print(f"There is no job called {target} (jobs: {known}).", file=sys.stderr)
            return 2
        before = max((r["id"] for r in outbox.recent(reg.conn, 1)), default=0)
        outcome = asyncio.run(host.scheduler.run_manual(app, name, force))
        print(f"{target}: {outcome.status}" + (f", {outcome.summary}" if outcome.summary else "")
              + (f" ({outcome.error})" if outcome.error else ""))
        for r in reg.conn.execute("SELECT * FROM outbox WHERE id > ? ORDER BY id", (before,)):
            print(f"queued for Elijah (sent at once if `jarvis serve` is running):\n{r['text']}")
        return 1 if outcome.status == "failed" else 0
    finally:
        host.close()
