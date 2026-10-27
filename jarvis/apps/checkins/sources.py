"""What a check-in tells the thinking turn: today's deadlines and reminders and the latest journal entry's open loops.

These come from the school, reminders and journal apps through ctx.call. An app that isn't installed just shows
as not available.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from jarvis.contract.api import AppContext, AppUnavailable


def _data(ctx: AppContext, app: str, action: str, **args) -> dict | None:
    try:
        return ctx.call(app, action, **args).data or {}
    except AppUnavailable:
        return None


def _hm(ctx: AppContext, iso: str) -> str:
    return f"{datetime.fromisoformat(iso).astimezone(ctx.tz):%a %-I:%M %p}"


def deadlines(ctx: AppContext, now: datetime, hours: int) -> list[str] | None:
    data = _data(ctx, "school", "deadlines_between", start=now.isoformat(),
                 end=(now + timedelta(hours=hours)).isoformat())
    if data is None:
        return None
    return [f"- {_hm(ctx, d['due'])} {d['course']}: {d['title']}" for d in data["deadlines"]
            if d["kind"] == "due" and not d["done"]]


def reminders(ctx: AppContext, now: datetime, hours: int) -> list[str] | None:
    data = _data(ctx, "reminders", "list_reminders", days=hours // 24 + 1)
    if data is None:
        return None
    end = now + timedelta(hours=hours)
    return [f"- {_hm(ctx, r['due_at'])}: {r['text']}" for r in data["reminders"]
            if datetime.fromisoformat(r["due_at"]) < end]


def done_today(ctx: AppContext, start: datetime, now: datetime) -> list[str] | None:
    """Reminders that went out since local midnight."""
    data = _data(ctx, "reminders", "sent_between", start=start.isoformat(),
                 end=(now + timedelta(seconds=1)).isoformat())
    if data is None:
        return None
    return [f"- reminder sent: {r['text']}" for r in data["reminders"]]


def latest_journal(ctx: AppContext) -> str | None:
    data = _data(ctx, "journal", "latest_entry")
    if not data:
        return None
    loops = data["open_loops"]
    return f"{data['day']}:\n{data['entry'][:900]}" + (f"\nOpen loops: {'; '.join(loops)}" if loops else "")
