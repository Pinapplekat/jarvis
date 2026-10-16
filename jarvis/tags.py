"""What Elijah is doing, one tag per saved screen: Jev picks a category from the window
(app, title, a bit of text); an app-name guess fills in without Jev. Tags add up into a
time breakdown for the journal and the evening check-in."""

import re
from collections import defaultdict
from datetime import datetime, timedelta

from jarvis.judge import Engine, Question

CATEGORIES = {
    "coding": "programming, terminals, code editors, debugging, GitHub",
    "homework": "schoolwork: Canvas, assignments, class readings, the class wiki, quizzes",
    "reading": "reading or researching articles, docs, wikis (not schoolwork)",
    "writing": "writing documents, notes, essays (not code)",
    "chatting": "Discord, Telegram, messaging apps, social chat",
    "email": "reading or writing email",
    "video": "YouTube, streaming, watching videos",
    "gaming": "playing or modding games, game launchers",
    "browsing": "general web browsing, shopping, social feeds",
    "planning": "calendars, to-do lists, planning, organizing",
    "other": "anything else",
}
GAP_CAP = timedelta(minutes=10)  # longest stretch one screen can be credited with

APP_HINTS = [
    (r"\b(code|cursor|xcode|terminal|iterm|warp|pycharm|intellij|github desktop|claude)\b", "coding"),
    (r"\b(discord|telegram|slack|whatsapp|signal)\b", "chatting"),
    (r"\b(mail|outlook|spark)\b", "email"),
    (r"\b(pages|word|notes|obsidian|notion)\b", "writing"),
    (r"\b(calendar|reminders|things|todoist)\b", "planning"),
    (r"\b(steam|minecraft|epic games|prism ?launcher)\b", "gaming"),
]
TITLE_HINTS = [
    (r"youtube|netflix|twitch|hulu|disney\+", "video"),
    (r"canvas|seattle colleges|assignment|quiz|module \d|youneedawiki|world.?building", "homework"),
    (r"github|stack overflow|localhost|\.py\b|\.ts\b|\.js\b", "coding"),
    (r"gmail|outlook", "email"),
    (r"docs\.google|google docs", "writing"),
    (r"reddit|twitter|x\.com|instagram|amazon", "browsing"),
]


def guess(app: str, title: str | None) -> str:
    """Tag from the app name and window title alone (no Jev)."""
    t = (title or "").lower()
    for pattern, tag in TITLE_HINTS:
        if re.search(pattern, t):
            return tag
    a = app.lower()
    for pattern, tag in APP_HINTS:
        if re.search(pattern, a):
            return tag
    if a in ("google chrome", "safari", "arc", "firefox", "microsoft edge", "brave browser"):
        return "browsing"
    return "other"


def tag_window(app: str, title: str | None, text: str, fast: Engine | None) -> str:
    if fast is None:
        return guess(app, title)
    state = f"App: {app}\nWindow title: {title or '?'}\nScreen text (start):\n{text[:600]}"
    question = Question("activity", "What is Elijah doing in this window? " +
                        "; ".join(f"{k} = {v}" for k, v in CATEGORIES.items()),
                        "choice", list(CATEGORIES))
    try:
        answer = fast(state, [question])["activity"]
    except Exception:
        return guess(app, title)
    return answer.value if answer.value in CATEGORIES else guess(app, title)


def time_by_tag(observations, end: datetime) -> dict[str, timedelta]:
    """Credit each screen observation with the time until the next one (capped)."""
    screens = [o for o in observations if o["source"] == "screen"]
    totals: dict[str, timedelta] = defaultdict(timedelta)
    for i, o in enumerate(screens):
        start = datetime.fromisoformat(o["at"])
        nxt = datetime.fromisoformat(screens[i + 1]["at"]) if i + 1 < len(screens) else end
        totals[o["tag"] or "other"] += max(timedelta(0), min(nxt - start, GAP_CAP))
    return dict(totals)


def describe(totals: dict[str, timedelta]) -> str:
    """'coding 2h10m, homework 45m, chatting 20m' (largest first, under a minute dropped)."""
    parts = []
    for tag, d in sorted(totals.items(), key=lambda kv: kv[1], reverse=True):
        minutes = int(d.total_seconds() // 60)
        if minutes < 1:
            continue
        parts.append(f"{tag} {minutes // 60}h{minutes % 60:02d}m" if minutes >= 60 else f"{tag} {minutes}m")
    return ", ".join(parts) or "(no screen time recorded)"
