"""What a check-in tells the thinking turn: today's deadlines and reminders and the latest journal entry's open loops.

These are read straight from the school, reminders and journal tables (read-only, and an app that isn't
installed just shows as not available).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from jarvis.contract.api import AppContext


def _utc(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat(timespec="seconds")


def _rows(ctx: AppContext, sql: str, params: tuple = ()) -> list[sqlite3.Row] | None:
    try:
        return ctx.db.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return None


def deadlines(ctx: AppContext, now: datetime, hours: int) -> list[str] | None:
    rows = _rows(ctx, "SELECT course, title, due, hand_done, status FROM school_deadlines WHERE kind='due' AND "
                      "julianday(due) >= julianday(?) AND julianday(due) < julianday(?) ORDER BY julianday(due)",
                 (_utc(now), _utc(now + timedelta(hours=hours))))
    if rows is None:
        return None
    return [f"- {datetime.fromisoformat(r['due']).astimezone(ctx.tz):%a %-I:%M %p} {r['course']}: {r['title']}"
            for r in rows if not (r["hand_done"] or r["status"] in ("submitted", "graded"))]


def reminders(ctx: AppContext, now: datetime, hours: int) -> list[str] | None:
    rows = _rows(ctx, "SELECT due_at, text FROM reminders_items WHERE status='pending' AND julianday(due_at) < "
                      "julianday(?) ORDER BY julianday(due_at)", (_utc(now + timedelta(hours=hours)),))
    if rows is None:
        return None
    return [f"- {datetime.fromisoformat(r['due_at']).astimezone(ctx.tz):%a %-I:%M %p}: {r['text']}" for r in rows]


def done_today(ctx: AppContext, start: datetime, now: datetime) -> list[str] | None:
    """Reminders that went out and deadlines that came due and were done, since local midnight."""
    sent = _rows(ctx, "SELECT text FROM reminders_items WHERE status='sent' AND sent_at >= ?", (_utc(start),))
    if sent is None:
        return None
    return [f"- reminder sent: {r['text']}" for r in sent]


def latest_journal(ctx: AppContext) -> str | None:
    rows = _rows(ctx, "SELECT day, entry, open_loops FROM journal_entries ORDER BY day DESC LIMIT 1")
    if not rows:
        return None
    r = rows[0]
    loops = json.loads(r["open_loops"])
    return f"{r['day']}:\n{r['entry'][:900]}" + (f"\nOpen loops: {'; '.join(loops)}" if loops else "")
