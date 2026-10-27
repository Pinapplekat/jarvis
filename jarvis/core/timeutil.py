"""Timestamps as stored in the database, and 'x min ago' for the prompt."""

from __future__ import annotations

from datetime import datetime, timezone


def stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")


def ago(when: str, now: datetime) -> str:
    try:
        seconds = int((now - datetime.fromisoformat(when)).total_seconds())
    except ValueError:
        return "earlier"
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{round(seconds / 60)} min ago"
    if seconds < 172800:
        return f"{round(seconds / 3600)} h ago"
    return f"{round(seconds / 86400)} days ago"
