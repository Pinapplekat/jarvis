"""Canvas deadlines: the Canvas API and calendar feed parsed into Deadline objects, and the digest. Standard library only."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

from jarvis.contract.api import ActionError

# Work with these submission types happens outside Canvas, so "unsubmitted" means nothing.
NO_CANVAS_SUBMISSION = {"none", "on_paper", "not_graded", "external_tool_manual"}
MODULE_DUE = re.compile(r"\(\s*DUE\s+([A-Z]{3})[A-Z]*\.?\s+(\d{1,2})\s*\)", re.IGNORECASE)
MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}
COURSE_CODE = re.compile(r"[A-Z]{2,5}\s?&?\s?\d{3}")
HTTP_TIMEOUT_S = 20


@dataclass
class Deadline:
    key: str  # stable id for nudge dedupe
    course: str
    title: str
    due: datetime  # timezone-aware
    url: str | None
    status: str  # submitted | graded | unsubmitted | missing | unknown
    trackable: bool  # False when Canvas can't tell us whether it's done
    kind: str = "due"  # due | event (calendar events aren't deadlines)

    @property
    def done(self) -> bool:
        return self.status in ("submitted", "graded")


# -- talking to Canvas ---------------------------------------------------------------------------------------------

def _get(opener, url: str, token: str | None = None) -> tuple[bytes, dict]:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with opener.open(request, timeout=HTTP_TIMEOUT_S) as response:
            return response.read(), dict(response.headers)
    except urllib.error.HTTPError as e:
        raise ActionError(f"{e.code} {e.reason}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ActionError(f"Canvas can't be reached ({getattr(e, 'reason', e)})") from None


def _next_link(headers: dict) -> str | None:
    link = next((v for k, v in headers.items() if k.lower() == "link"), "")
    m = re.search(r'<([^>]+)>\s*;\s*rel="next"', link)
    return m[1] if m else None


class CanvasClient:
    def __init__(self, base_url: str, token: str, opener=None) -> None:
        self.base, self.token = base_url.rstrip("/") + "/api/v1", token
        self.opener = opener or urllib.request.build_opener()

    def get_all(self, path: str, **params) -> list[dict]:
        url = f"{self.base}{path}?" + urllib.parse.urlencode({"per_page": 100, **params}, doseq=True)
        out: list[dict] = []
        while url:
            body, headers = _get(self.opener, url, self.token)
            out.extend(json.loads(body))
            url = _next_link(headers)
        return out

    def fetch_deadlines(self, tz: tzinfo, today: date) -> list[Deadline]:
        deadlines: list[Deadline] = []
        for course in self.get_all("/courses", enrollment_state="active"):
            if "access_restricted_by_date" in course:
                continue
            name = course.get("course_code") or course.get("name", str(course["id"]))
            deadlines += from_assignments(
                name, self.get_all(f"/courses/{course['id']}/assignments", **{"include[]": "submission"}))
            deadlines += from_modules(name, course["id"], self.get_all(f"/courses/{course['id']}/modules"), tz, today)
        return sorted(deadlines, key=lambda d: d.due)


def fetch_feed(url: str, opener=None) -> bytes:
    return _get(opener or urllib.request.build_opener(), url)[0]


# -- turning what Canvas says into deadlines ----------------------------------------------------------------------

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
            key=f"canvas:{a['id']}", course=course, title=a["name"],
            due=datetime.fromisoformat(a["due_at"].replace("Z", "+00:00")), url=a.get("html_url"),
            status=status if trackable else "unknown", trackable=trackable,
        ))
    return out


def from_modules(course: str, course_id: int, modules: list[dict], tz: tzinfo, today: date) -> list[Deadline]:
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
            key=f"module:{course_id}:{m['id']}", course=course, title=MODULE_DUE.sub("", m["name"]).strip(" :-"),
            due=datetime.combine(due_day, time(23, 59), tz), url=None, status="unknown", trackable=False,
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


FEED_UID = re.compile(r"event-(assignment|calendar-event)-(\d+)")
FEED_COURSE = re.compile(r"\s*\[([^\]]+)\]\s*$")


def _ics_events(ics: bytes) -> list[dict]:
    """The VEVENTs of an iCalendar file as {NAME: (params, value)} (just what the Canvas feed uses)."""
    lines: list[str] = []
    for raw in ics.decode("utf-8", "replace").splitlines():
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]  # a folded line continues the one before
        else:
            lines.append(raw)
    events, current = [], None
    for line in lines:
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT" and current is not None:
            events.append(current)
            current = None
        elif current is not None and ":" in line:
            head, _, value = line.partition(":")
            name, *params = head.split(";")
            current[name.upper()] = (dict(p.partition("=")[::2] for p in params), value)
    return events


def _ics_text(value: str) -> str:
    return re.sub(r"\\([,;nN\\])", lambda m: "\n" if m[1] in "nN" else m[1], value)


def _ics_when(params: dict, value: str, tz: tzinfo) -> datetime:
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):  # all-day: due end of day
        return datetime.combine(datetime.strptime(value, "%Y%m%d").date(), time(23, 59), tz)
    when = datetime.strptime(value.rstrip("Z"), "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        return when.replace(tzinfo=timezone.utc)
    return when.replace(tzinfo=ZoneInfo(params["TZID"]) if "TZID" in params else tz)


def from_feed(ics: bytes, tz: tzinfo) -> list[Deadline]:
    """Parse Canvas's personal calendar feed. It has dates but no submission status."""
    out = []
    for ev in _ics_events(ics):
        if "DTSTART" not in ev:
            continue
        start = _ics_when(*ev["DTSTART"], tz)
        summary = _ics_text(ev.get("SUMMARY", ({}, ""))[1]).strip()
        course_match = FEED_COURSE.search(summary)
        course = course_match[1] if course_match else "Canvas"
        title = FEED_COURSE.sub("", summary)
        uid = ev.get("UID", ({}, ""))[1]
        m = FEED_UID.search(uid)
        is_assignment = bool(m and m[1] == "assignment")
        key = f"canvas:{m[2]}" if is_assignment else f"feed:{uid or summary}"
        url = ev.get("URL", ({}, ""))[1]
        out.append(Deadline(key=key, course=_short_course(course), title=title, due=start, url=url or None,
                            status="unknown", trackable=False, kind="due" if is_assignment else "event"))
    return sorted(out, key=lambda d: d.due)


def _short_course(name: str) -> str:
    """'ENGL&101 26 35938' -> 'ENGL&101'; 'ECON 100 - Intro - Fall 2026' -> 'ECON 100'."""
    m = COURSE_CODE.match(name.strip())
    return m[0] if m else name.split(" - ")[0].strip()


# -- names, duplicates, and the digest ----------------------------------------------------------------------------

def course_name(course: str, codes: set[str]) -> str:
    """One name per class: a hand-typed 'English' becomes Canvas's 'ENGL&101' when exactly one Canvas
    course has that subject. Anything else is left as typed."""
    if COURSE_CODE.fullmatch(course):
        return course
    word = course.strip().upper()
    hits = {c for c in codes if (subj := re.match(r"[A-Z]+", c)[0]) and len(word) >= 3
            and (word.startswith(subj) or subj.startswith(word[:4]))}
    return hits.pop() if len(hits) == 1 else course


SUBJECTS = {"ENGL": "English", "CMST": "Communication", "ECON": "Econ", "MATH": "Math", "PSYC": "Psychology",
            "BIOL": "Biology", "CHEM": "Chemistry", "HIST": "History", "PHYS": "Physics", "SOC": "Sociology",
            "PHIL": "Philosophy", "ART": "Art", "CS": "CS", "CSC": "CS", "MUSC": "Music", "SPAN": "Spanish"}


def spoken_course(course: str, typed: dict[str, str] | None = None) -> str:
    """'ENGL&101' -> what he calls it: his own name for it if there is one, else the subject."""
    if typed and course in typed:
        return typed[course]
    m = re.match(r"([A-Z]+)\s?&?\s?\d", course)
    return SUBJECTS.get(m[1], course) if m else course


def _plain_title(title: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", re.sub(r"\([^)]*\)", "", title.lower())).split())


def same_work(a: Deadline, b: Deadline) -> bool:
    """The same assignment listed twice: same class, same title give or take a note in brackets or a
    trailing word, due within a week of each other."""
    ta, tb = _plain_title(a.title), _plain_title(b.title)
    close = abs(a.due - b.due) <= timedelta(days=7)
    return a.course == b.course and close and min(len(ta), len(tb)) >= 6 and (ta.startswith(tb) or tb.startswith(ta))


def merge(primary: list[Deadline], overrides: list[Deadline]) -> list[Deadline]:
    """Overrides replace primary items with the same key. Class names are made to agree, and a hand-added
    item that repeats one Canvas already has is dropped: Canvas's date wins, the hand-kept 'done' carries over."""
    codes = {d.course for d in primary + overrides if COURSE_CODE.fullmatch(d.course)}
    primary = [replace(d, course=course_name(d.course, codes)) for d in primary]
    overrides = [replace(d, course=course_name(d.course, codes)) for d in overrides]
    by_key = {d.key: d for d in primary}
    by_key.update({d.key: d for d in overrides})
    for o in overrides:
        if o.key.startswith("canvas:") or o.kind != "due":
            continue
        twin = next((d for d in by_key.values() if d.key.startswith("canvas:") and d.kind == "due"
                     and same_work(d, o)), None)
        if twin:
            del by_key[o.key]
            if o.done and not twin.done:
                by_key[twin.key] = replace(twin, status="submitted")
    return sorted(by_key.values(), key=lambda d: d.due)


def day_word(due: datetime, now: datetime) -> str:
    gap = (due.astimezone(now.tzinfo).date() - now.date()).days
    return "today" if gap == 0 else "tomorrow" if gap == 1 else due.astimezone(now.tzinfo).strftime("%a")


def format_digest(deadlines: list[Deadline], now: datetime, days: int = 7) -> str:
    """What's coming, in Falak's words. Each line ends with the item's key (for mark_done)."""
    end = now + timedelta(days=days)
    upcoming = [d for d in deadlines if now <= d.due < end]
    missing = [d for d in deadlines if d.status == "missing"]
    lines = [f"The week ahead, {now:%a %b %-d} to {(end - timedelta(days=1)):%a %b %-d}:"]
    if missing:
        lines.append(f"Missing ({len(missing)}):")
        lines += [f"  {spoken_course(d.course)}: {d.title}, was due {d.due.astimezone(now.tzinfo):%b %-d} [{d.key}]"
                  for d in missing]
    by_day: dict[date, list[Deadline]] = defaultdict(list)
    for d in upcoming:
        by_day[d.due.astimezone(now.tzinfo).date()].append(d)
    for day in sorted(by_day):
        lines.append(f"{day:%a %b %-d}:")
        for d in by_day[day]:
            at = f"{d.due.astimezone(now.tzinfo):%-I:%M %p}"
            if d.kind == "event":
                lines.append(f"  {at} {spoken_course(d.course)}: {d.title} (an event, not a deadline) [{d.key}]")
                continue
            note = " (done)" if d.done else "" if d.trackable else " (Canvas can't tell if it's done)"
            lines.append(f"  {at} {spoken_course(d.course)}: {d.title}{note} [{d.key}]")
    if not upcoming:
        lines.append("Nothing due.")
    due = [d for d in upcoming if d.kind == "due"]
    lines.append(f"{sum(1 for d in due if not d.done)} of {len(due)} still to do.")
    return "\n".join(lines)
