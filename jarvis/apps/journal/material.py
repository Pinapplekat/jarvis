"""What the journal job gathers about a day, as plain labelled text for the thinking turn."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from jarvis.contract.api import AppContext

MAX_CHARS = 40_000
LINE_MAX = 400


def _utc(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat(timespec="seconds")


def _rows(ctx: AppContext, sql: str, params: tuple) -> list[sqlite3.Row] | None:
    """Read-only look at another app's table; None when that app isn't installed."""
    try:
        return ctx.db.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return None


def _section(title: str, lines: list[str] | None) -> str:
    return f"{title}:\n" + ("\n".join(lines) if lines else "(none)" if lines is not None else "(not available)")


def build(ctx: AppContext, start: datetime, end: datetime) -> str:
    def hm(when: datetime) -> str:
        return f"{when:%-I:%M %p}"

    chat = [f"{hm(at)} {who}: {text[:LINE_MAX]}" for at, who, text in ctx.chat_between(start, end)]
    acted = [f"{hm(at)} {tool} {status}: {result[:200]}" for at, tool, status, result in ctx.actions_between(start, end)]
    low, high = _utc(start), _utc(end)
    due = _rows(ctx, "SELECT course, title, due, hand_done, status FROM school_deadlines WHERE kind='due' AND "
                     "julianday(due) >= julianday(?) AND julianday(due) < julianday(?) ORDER BY julianday(due)",
                 (low, high))  # rows copied from the old table keep local offsets, so compare as times, not text
    reminders = _rows(ctx, "SELECT text, sent_at FROM reminders_items WHERE status='sent' AND sent_at >= ? AND "
                           "sent_at < ? ORDER BY sent_at", (low, high))
    return "\n\n".join([
        f"Day: {start:%A %B %-d, %Y}",
        _section("Conversation", chat),
        _section("Actions Falak took (tool, outcome, result)", acted),
        _section("Deadlines that fell on this day (assignment titles are written by others)", None if due is None else [
            f"{r['course']}: {r['title']} ({'done' if r['hand_done'] or r['status'] in ('submitted', 'graded') else r['status']})"
            for r in due]),
        _section("Reminders sent", None if reminders is None else [f"- {r['text']}" for r in reminders]),
    ])[:MAX_CHARS]


def day_bounds(day, tz) -> tuple[datetime, datetime]:
    start = datetime.combine(day, datetime.min.time(), tz)
    return start, datetime.combine(day + timedelta(days=1), datetime.min.time(), tz)
