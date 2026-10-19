"""Live web search through an official search API (Brave Search by default, or Google
Programmable Search), never by scraping a results page.

The chat model only proposes a query; JARVIS cleans it, runs it here and hands the results back
as untrusted data. Queries leave the Mac, so emails and links are stripped from them first, and
a daily cap keeps the free tier's quota safe. Each query is logged (the journal picks it up).
"""

import html
import re
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from jarvis import config
from jarvis.memory import Memory

BRAVE = "https://api.search.brave.com/res/v1/web/search"
GOOGLE = "https://www.googleapis.com/customsearch/v1"
TIMEOUT = 8
MAX_QUERY = 120
MAX_TITLE, MAX_SNIPPET, MAX_URL = 120, 300, 300

_EMAIL = re.compile(r"\S+@\S+")
_LINK = re.compile(r"\b(?:https?://|www\.)\S+", re.I)
_TAG = re.compile(r"<[^>]*>")


@dataclass
class Found:
    query: str
    results: list[dict] = field(default_factory=list)  # each {title, snippet, url}
    error: str = ""  # "" = the search ran; otherwise why it's unavailable


def clean_query(query: str) -> str:
    """What may leave the Mac: no email addresses or links (a Canvas feed link is a password), short."""
    q = _LINK.sub(" ", _EMAIL.sub(" ", str(query)))
    return " ".join(q.split())[:MAX_QUERY].strip()


def _text(value, limit: int) -> str:
    """Plain, short text: the API's <strong> highlights and entities removed, and no angle brackets
    left that could pose as one of JARVIS's own blocks."""
    s = " ".join(html.unescape(_TAG.sub("", str(value or ""))).split())
    s = s.replace("<", "‹").replace(">", "›")
    return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"


def _result(title, snippet, url) -> dict | None:
    url = str(url or "").strip()
    if not url.lower().startswith(("https://", "http://")):
        return None
    return {"title": _text(title, MAX_TITLE) or "(untitled)", "snippet": _text(snippet, MAX_SNIPPET),
            "url": url[:MAX_URL]}


def search(query: str, n: int = 5, get=None) -> Found:
    """Top `n` results for `query` from the configured provider. Never raises: a missing key, no
    network, a bad key or a rate limit comes back as Found.error."""
    get = get or httpx.get
    q, n = clean_query(query), max(1, min(int(n), 10))
    if not q:
        return Found(q, error="search unavailable: empty query")
    provider = config.SEARCH_PROVIDER.lower()
    if not config.SEARCH_API_KEY:
        return Found(q, error="search unavailable: no SEARCH_API_KEY set")
    try:
        if provider == "brave":
            r = get(BRAVE, params={"q": q, "count": n},
                    headers={"Accept": "application/json", "X-Subscription-Token": config.SEARCH_API_KEY},
                    timeout=TIMEOUT)
        elif provider == "google":
            if not config.SEARCH_ENGINE_ID:
                return Found(q, error="search unavailable: no SEARCH_ENGINE_ID set for Google")
            r = get(GOOGLE, params={"key": config.SEARCH_API_KEY, "cx": config.SEARCH_ENGINE_ID, "q": q, "num": n},
                    timeout=TIMEOUT)
        else:
            return Found(q, error=f"search unavailable: unknown SEARCH_PROVIDER {provider!r}")
    except httpx.TimeoutException:
        return Found(q, error="search unavailable: the search service didn't answer in time")
    except httpx.HTTPError:
        return Found(q, error="search unavailable: no network")
    if r.status_code in (401, 403):
        return Found(q, error="search unavailable: the search API key was refused")
    if r.status_code == 429:
        return Found(q, error="search unavailable: rate limit or quota reached")
    if r.status_code != 200:
        return Found(q, error=f"search unavailable: the search service answered {r.status_code}")
    try:
        data = r.json()
    except ValueError:
        return Found(q, error="search unavailable: unreadable answer from the search service")
    if provider == "brave":
        raw = [(i.get("title"), i.get("description"), i.get("url"))
               for i in (data.get("web") or {}).get("results") or [] if isinstance(i, dict)]
    else:
        raw = [(i.get("title"), i.get("snippet"), i.get("link")) for i in data.get("items") or [] if isinstance(i, dict)]
    return Found(q, [x for x in (_result(*t) for t in raw) if x][:n])


def used_today(mem: Memory, now: datetime | None = None) -> int:
    from jarvis.journal import day_bounds
    now = now or datetime.now(ZoneInfo(config.TIMEZONE))
    start, end = day_bounds(now.date(), now.tzinfo)
    return sum(1 for e in mem.events_between(start, end) if e["kind"] == "web_search")


def run(mem: Memory, query: str, n: int = 5, get=None, now: datetime | None = None) -> Found:
    """search() with the daily cap, logging each query (never the results)."""
    if not config.WEB_SEARCH:
        return Found(clean_query(query), error="search unavailable: web search is turned off (WEB_SEARCH=0)")
    if used_today(mem, now) >= config.SEARCH_DAILY_LIMIT:
        return Found(clean_query(query), error=f"search unavailable: today's cap of {config.SEARCH_DAILY_LIMIT} "
                                                "searches is used up")
    found = search(query, n, get)
    if found.query:
        mem.log("web_search", f"{found.query}: {found.error or f'{len(found.results)} results'}"[:250])
    return found


def for_prompt(found: Found) -> str:
    if found.error:
        return f"({found.error}. You did not search: don't claim you did, and say you couldn't look it up.)"
    if not found.results:
        return "(the search ran but found nothing)"
    return "\n".join(f"{i}. {r['title']}\n   {r['snippet']}\n   {r['url']}" for i, r in enumerate(found.results, 1))
