"""Canvas through Falak's browser: the same API requests the token client makes, answered by the browser app's
`fetch_json` (a GET in Elijah's signed-in profile), plus the announcements and the module dates that need no token."""

from __future__ import annotations

import html
import re
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo

from jarvis.apps.school import canvas
from jarvis.apps.school.canvas import Deadline
from jarvis.contract.api import AppContext

MAX_PAGES = 20  # of one list; a runaway Link header must not loop
ANNOUNCEMENT_DAYS = 14
MOVES_A_DATE = re.compile(
    r"\b(extend(?:ed|s|ing)?|extension|postpone[ds]?|reschedul\w*|pushed\s+(?:back|to)|moved\s+(?:to|back)|now\s+due|"
    r"new\s+(?:due\s+)?dates?|due\s+date\s+(?:has\s+)?(?:changed|moved)|deadline\s+(?:has\s+)?(?:changed|moved)|"
    r"delayed)\b", re.I)
_TAG = re.compile(r"<[^>]*>")
_STOP = {"module", "chapter", "chapters", "week", "with", "from", "that", "this", "your", "and", "the"}


class SignedOut(Exception):
    """Canvas redirected to a sign-in page, or the browser has no login for it."""


@dataclass
class Notice:
    id: str
    course: str
    title: str
    body: str
    posted_at: datetime


class BrowserClient(canvas.CanvasClient):
    """CanvasClient with the transport swapped: every page of every list goes through the browser app."""

    def __init__(self, ctx: AppContext, base_url: str) -> None:
        self.ctx, self.base = ctx, base_url.rstrip("/") + "/api/v1"
        self._courses: list[dict] | None = None

    def get_all(self, path: str, **params) -> list[dict]:
        url: str | None = f"{self.base}{path}?" + urllib.parse.urlencode({"per_page": 100, **params}, doseq=True)
        out: list[dict] = []
        for _ in range(MAX_PAGES):
            if not url:
                break
            data = self.ctx.call("browser", "fetch_json", url=url).data or {}  # AppUnavailable goes to the caller
            if data.get("signed_out"):
                raise SignedOut(data.get("host", ""))
            body = data.get("json")
            out.extend(body if isinstance(body, list) else [])
            url = data.get("next")
        return out

    def courses(self) -> list[dict]:
        if self._courses is None:
            self._courses = [c for c in self.get_all("/courses", enrollment_state="active")
                             if "access_restricted_by_date" not in c]
        return self._courses

    def fetch_deadlines(self, tz: tzinfo, today: date) -> list[Deadline]:
        found: list[Deadline] = []
        modules: list[Deadline] = []
        for course in self.courses():
            name = course.get("course_code") or course.get("name", str(course["id"]))
            found += canvas.from_assignments(
                name, self.get_all(f"/courses/{course['id']}/assignments", **{"include[]": "submission"}))
            modules += canvas.from_modules(name, course["id"], self.get_all(f"/courses/{course['id']}/modules"),
                                           tz, today)
        found += [m for m in modules if not covered(m, found)]
        return sorted(found, key=lambda d: d.due)

    def fetch_notices(self, today: date) -> list[Notice]:
        names = {f"course_{c['id']}": c.get("course_code") or c.get("name", "") for c in self.courses()}
        if not names:
            return []
        start = (today - timedelta(days=ANNOUNCEMENT_DAYS)).isoformat()
        out = []
        for a in self.get_all("/announcements", **{"context_codes[]": list(names), "start_date": start}):
            text = plain(f"{a.get('title', '')}. {a.get('message', '')}")
            if MOVES_A_DATE.search(text) and a.get("posted_at"):
                out.append(Notice(f"canvas:{a['id']}", canvas._short_course(names.get(a.get("context_code", ""), "")),
                                  plain(a.get("title", ""))[:160], plain(a.get("message", ""))[:400],
                                  datetime.fromisoformat(a["posted_at"].replace("Z", "+00:00"))))
        return out


def plain(text: str) -> str:
    return " ".join(html.unescape(_TAG.sub(" ", str(text))).split())


def _words(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{4,}", title.lower()) if w not in _STOP}


def covered(module: Deadline, assignments: list[Deadline]) -> bool:
    """A module's own date is dropped when an assignment of the same class already gives that day for the same work:
    the title names the same module ("Module 2"), or shares two real words with it."""
    label = re.match(r"\s*(module|week|unit)\s*(\d+)", module.title, re.I)
    for a in assignments:
        if a.course != module.course or a.due.astimezone(module.due.tzinfo).date() != module.due.date():
            continue
        if label and re.search(rf"\b{label[1]}\s*{label[2]}\b", a.title, re.I):
            return True
        if len(_words(module.title) & _words(a.title)) >= 2:
            return True
    return False
