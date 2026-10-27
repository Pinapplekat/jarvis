"""Timestamps as stored in the database, 'x min ago' for the prompt, and the clock rules the scheduler uses."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone, tzinfo

from jarvis.contract.timespec import parse_clock


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


def parse_quiet_hours(spec: str) -> tuple[time, time] | None:
    """'23-7' -> (23:00, 07:00). An empty string turns quiet hours off. Raises ValueError for anything else."""
    if not spec.strip():
        return None
    start, end = (int(h) for h in spec.split("-"))
    return time(start), time(end)


def in_quiet_hours(now: time, window: tuple[time, time] | None) -> bool:
    if window is None:
        return False
    start, end = window
    if start <= end:
        return start <= now < end
    return now >= start or now < end


def quiet_ends(now: datetime, window: tuple[time, time] | None) -> datetime | None:
    """When the quiet hours `now` is inside end (the next time the clock reads the window's end), else None."""
    if window is None or not in_quiet_hours(now.time().replace(tzinfo=None), window):
        return None
    return _at_or_after(now, window[1], strictly=True)


def _local(day: date, at: time, tz: tzinfo) -> datetime:
    """A wall-clock time on a day, normalized through UTC so a skipped DST hour lands on a real instant."""
    return datetime.combine(day, at, tz).astimezone(timezone.utc).astimezone(tz)


def _at_or_after(after: datetime, at: time, strictly: bool) -> datetime:
    for offset in range(3):
        candidate = _local(after.date() + timedelta(days=offset), at, after.tzinfo)
        # compare instants: two times in the same zone compare by wall clock, which is wrong in a repeated hour
        gap = candidate.astimezone(timezone.utc) - after.astimezone(timezone.utc)
        if gap > timedelta(0) or (not strictly and gap == timedelta(0)):
            return candidate
    raise AssertionError("unreachable")


def next_clock(after: datetime, times: list[time]) -> datetime:
    """The next of these local times of day strictly after `after` (aware, in the local timezone)."""
    return min(_at_or_after(after, t, strictly=True) for t in times)


def clock_times(texts: list[str]) -> list[time]:
    return [t for t in (parse_clock(x) for x in texts) if t is not None]
