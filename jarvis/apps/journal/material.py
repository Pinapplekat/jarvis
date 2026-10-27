"""What the journal job gathers about a day, as plain labelled text for the thinking turn."""

from __future__ import annotations

from datetime import datetime, timedelta

from jarvis.contract.api import AppContext, AppUnavailable

MAX_CHARS = 40_000
LINE_MAX = 400


def _data(ctx: AppContext, app: str, action: str, **args) -> dict | None:
    """Another app's shared read; None when that app isn't installed."""
    try:
        return ctx.call(app, action, **args).data or {}
    except AppUnavailable:
        return None


def _section(title: str, lines: list[str] | None) -> str:
    return f"{title}:\n" + ("\n".join(lines) if lines else "(none)" if lines is not None else "(not available)")


def build(ctx: AppContext, start: datetime, end: datetime) -> str:
    def hm(when: datetime) -> str:
        return f"{when:%-I:%M %p}"

    chat = [f"{hm(at)} {who}: {text[:LINE_MAX]}" for at, who, text in ctx.chat_between(start, end)]
    acted = [f"{hm(at)} {tool} {status}: {result[:200]}" for at, tool, status, result in ctx.actions_between(start, end)]
    due = _data(ctx, "school", "deadlines_between", start=start.isoformat(), end=end.isoformat())
    reminders = _data(ctx, "reminders", "sent_between", start=start.isoformat(), end=end.isoformat())
    return "\n\n".join([
        f"Day: {start:%A %B %-d, %Y}",
        _section("Conversation", chat),
        _section("Actions Falak took (tool, outcome, result)", acted),
        _section("Deadlines that fell on this day (assignment titles are written by others)", None if due is None else [
            f"{d['course']}: {d['title']} ({'done' if d['done'] else d['status']})"
            for d in due["deadlines"] if d["kind"] == "due"]),
        _section("Reminders sent", None if reminders is None else [f"- {r['text']}" for r in reminders["reminders"]]),
    ])[:MAX_CHARS]


def day_bounds(day, tz) -> tuple[datetime, datetime]:
    start = datetime.combine(day, datetime.min.time(), tz)
    return start, datetime.combine(day + timedelta(days=1), datetime.min.time(), tz)
