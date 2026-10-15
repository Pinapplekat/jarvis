"""Canvas deadlines, deadline alerts, and the weekly digest."""

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
import icalendar

from jarvis import config
from jarvis.memory import Memory

# Work with these submission types happens outside Canvas, so "unsubmitted" means nothing.
NO_CANVAS_SUBMISSION = {"none", "on_paper", "not_graded", "external_tool_manual"}
MODULE_DUE = re.compile(r"\(\s*DUE\s+([A-Z]{3})[A-Z]*\.?\s+(\d{1,2})\s*\)", re.IGNORECASE)
MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


@dataclass
class Deadline:
    key: str                # stable id for alert dedupe
    course: str
    title: str
    due: datetime           # timezone-aware
    url: str | None
    status: str             # submitted | graded | unsubmitted | missing | unknown
    trackable: bool         # False when Canvas can't tell us whether it's done
    kind: str = "due"       # due | event (calendar events aren't deadlines)

    @property
    def done(self) -> bool:
        return self.status in ("submitted", "graded")


class CanvasClient:
    def __init__(self, base_url: str = config.CANVAS_BASE_URL, token: str = config.CANVAS_TOKEN):
        if not base_url or not token:
            raise SystemExit("Set CANVAS_BASE_URL and CANVAS_TOKEN in .env")
        self.http = httpx.Client(
            base_url=f"{base_url.rstrip('/')}/api/v1",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )

    def get_all(self, path: str, **params) -> list[dict]:
        out, url = [], path
        params = {"per_page": 100, **params}
        while url:
            r = self.http.get(url, params=params)
            r.raise_for_status()
            out.extend(r.json())
            url = r.links.get("next", {}).get("url")
            params = None  # the next link already carries them
        return out

    def fetch_deadlines(self, tz: ZoneInfo, today: date) -> list[Deadline]:
        deadlines = []
        for course in self.get_all("/courses", enrollment_state="active"):
            if "access_restricted_by_date" in course:
                continue
            name = course.get("course_code") or course.get("name", str(course["id"]))
            assignments = self.get_all(f"/courses/{course['id']}/assignments", **{"include[]": "submission"})
            deadlines += from_assignments(name, assignments)
            modules = self.get_all(f"/courses/{course['id']}/modules")
            deadlines += from_modules(name, course["id"], modules, tz, today)
        return sorted(deadlines, key=lambda d: d.due)


def from_assignments(course: str, assignments: list[dict]) -> list[Deadline]:
    out = []
    for a in assignments:
        if not a.get("due_at"):
            continue
        sub = a.get("submission") or {}
        if sub.get("missing"):
            status = "missing"
        else:
            status = sub.get("workflow_state", "unknown")
            if status == "pending_review":
                status = "submitted"
        trackable = not set(a.get("submission_types", [])) <= NO_CANVAS_SUBMISSION
        out.append(Deadline(
            key=f"canvas:{a['id']}",
            course=course,
            title=a["name"],
            due=datetime.fromisoformat(a["due_at"].replace("Z", "+00:00")),
            url=a.get("html_url"),
            status=status if trackable else "unknown",
            trackable=trackable,
        ))
    return out


FEED_UID = re.compile(r"event-(assignment|calendar-event)-(\d+)")
FEED_COURSE = re.compile(r"\s*\[([^\]]+)\]\s*$")


def fetch_feed(url: str) -> bytes:
    r = httpx.get(url, timeout=30, follow_redirects=True)
    r.raise_for_status()
    return r.content


def from_feed(ics: bytes, tz: ZoneInfo) -> list[Deadline]:
    """Parse Canvas's personal calendar feed. It has dates but no submission status."""
    cal = icalendar.Calendar.from_ical(ics)
    out = []
    for ev in cal.walk("VEVENT"):
        start = ev.decoded("DTSTART", None)
        if start is None:
            continue
        if not isinstance(start, datetime):  # all-day item: treat as due end of day
            start = datetime.combine(start, time(23, 59), tz)
        elif start.tzinfo is None:
            start = start.replace(tzinfo=tz)

        summary = str(ev.get("SUMMARY", "")).strip()
        course_match = FEED_COURSE.search(summary)
        course = course_match[1] if course_match else "Canvas"
        title = FEED_COURSE.sub("", summary)

        uid = str(ev.get("UID", ""))
        m = FEED_UID.search(uid)
        is_assignment = bool(m and m[1] == "assignment")
        key = f"canvas:{m[2]}" if is_assignment else f"feed:{uid or summary}"
        url = ev.get("URL")
        out.append(Deadline(
            key=key, course=_short_course(course), title=title, due=start,
            url=str(url) if url else None, status="unknown", trackable=False,
            kind="due" if is_assignment else "event",
        ))
    return sorted(out, key=lambda d: d.due)


COURSE_CODE = re.compile(r"[A-Z]{2,5}\s?&?\s?\d{3}")


def _short_course(name: str) -> str:
    """'ENGL&101 26 35938' -> 'ENGL&101'; 'ECON 100 - Intro - Fall 2026' -> 'ECON 100'."""
    m = COURSE_CODE.match(name.strip())
    return m[0] if m else name.split(" - ")[0].strip()


def from_modules(course: str, course_id: int, modules: list[dict], tz: ZoneInfo, today: date) -> list[Deadline]:
    """Modules titled like 'Module 2: Chapters 3 & 4 (DUE OCT 11)' are due 11:59 PM that day."""
    out = []
    for m in modules:
        match = MODULE_DUE.search(m.get("name", ""))
        if not match or match[1].upper() not in MONTHS:
            continue
        due_day = _infer_year(MONTHS[match[1].upper()], int(match[2]), today)
        if due_day is None:
            continue
        out.append(Deadline(
            key=f"module:{course_id}:{m['id']}",
            course=course,
            title=MODULE_DUE.sub("", m["name"]).strip(" :-"),
            due=datetime.combine(due_day, time(23, 59), tz),
            url=None,
            status="unknown",
            trackable=False,
        ))
    return out


def _infer_year(month: int, day: int, today: date) -> date | None:
    """Module titles have no year: pick the one that puts the date nearest to today."""
    options = []
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            options.append(date(year, month, day))
        except ValueError:
            pass
    return min(options, key=lambda d: abs(d - today)) if options else None


def raise_deadline_alerts(mem: Memory, deadlines: list[Deadline], now: datetime) -> int:
    """Text 24h before anything not yet done; call 3h before trackable work still unsubmitted."""
    raised = 0
    for d in deadlines:
        left = d.due - now
        if d.kind != "due" or d.done or left <= timedelta(0):
            continue
        when = d.due.astimezone(now.tzinfo).strftime("%-I:%M %p")
        if left <= timedelta(hours=3) and d.trackable:
            msg = f"{d.course}: {d.title} is due at {when} and isn't submitted."
            raised += mem.raise_alert("urgent", msg, f"{d.key}:3h") is not None
        elif left <= timedelta(hours=24):
            check = "" if d.trackable else " (check it's done)"
            msg = f"{d.course}: {d.title} due {_day_word(d.due, now)} at {when}{check}."
            raised += mem.raise_alert("high", msg, f"{d.key}:24h") is not None
    return raised


def _day_word(due: datetime, now: datetime) -> str:
    gap = (due.astimezone(now.tzinfo).date() - now.date()).days
    return "today" if gap == 0 else "tomorrow" if gap == 1 else due.astimezone(now.tzinfo).strftime("%a")


def format_digest(deadlines: list[Deadline], now: datetime, days: int = 7) -> str:
    end = now + timedelta(days=days)
    upcoming = [d for d in deadlines if now <= d.due < end]
    missing = [d for d in deadlines if d.status == "missing"]

    lines = [f"JARVIS week ahead ({now:%a %b %-d} - {end - timedelta(days=1):%a %b %-d})"]
    if missing:
        lines.append(f"\nMISSING ({len(missing)}):")
        lines += [f"  ! {d.course}: {d.title} (was due {d.due.astimezone(now.tzinfo):%b %-d})" for d in missing]

    by_day: dict[date, list[Deadline]] = defaultdict(list)
    for d in upcoming:
        by_day[d.due.astimezone(now.tzinfo).date()].append(d)
    for day in sorted(by_day):
        lines.append(f"\n{day:%a %b %-d}")
        for d in by_day[day]:
            at = f"{d.due.astimezone(now.tzinfo):%-I:%M%p}"
            if d.kind == "event":
                lines.append(f"   -  {at} {d.course}: {d.title}")
                continue
            mark = "x" if d.done else "?" if not d.trackable else " "
            lines.append(f"  [{mark}] {at} {d.course}: {d.title}")
    if not upcoming:
        lines.append("\nNothing due this week.")

    due = [d for d in upcoming if d.kind == "due"]
    todo = sum(1 for d in due if not d.done)
    lines.append(f"\n{todo} of {len(due)} still to do. [?] = Canvas can't see if it's done; check yourself.")
    return "\n".join(lines)
