"""Web search through an official search API (Brave by default, or Google Programmable Search), and reading pages.

The model only proposes a query or a link. Queries leave the Mac, so email addresses and links are stripped first,
and a daily cap keeps the free tier's quota safe. Results and pages are someone else's words: untrusted output.
"""

from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, time, timezone

from jarvis.apps.web import page
from jarvis.contract.api import ActionError, AppContext, Result

BRAVE = "https://api.search.brave.com/res/v1/web/search"
GOOGLE = "https://www.googleapis.com/customsearch/v1"
TIMEOUT = 10
MAX_QUERY = 120
MAX_TITLE, MAX_SNIPPET, MAX_URL = 120, 300, 300

_EMAIL = re.compile(r"\S+@\S+")
_LINK = re.compile(r"\b(?:https?://|www\.)\S+", re.I)
_TAG = re.compile(r"<[^>]*>")


def _opener():
    return urllib.request.build_opener()


def _page_opener(ctx: AppContext):
    return page.opener(lambda url: _check(ctx, url)).open


def _check(ctx: AppContext, url: str) -> str | None:
    return page.blocked_site(url, str(ctx.settings["blocklist"])) or page.check_url(url)


def clean_query(query: str) -> str:
    """What may leave the Mac: no email addresses or links (a Canvas feed link is a password), and short."""
    q = _LINK.sub(" ", _EMAIL.sub(" ", str(query)))
    return " ".join(q.split())[:MAX_QUERY].strip()


def _text(value, limit: int) -> str:
    s = " ".join(html.unescape(_TAG.sub("", str(value or ""))).split())
    return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"


def _result(title, snippet, url) -> dict | None:
    url = str(url or "").strip()
    if not url.lower().startswith(("https://", "http://")):
        return None
    return {"title": _text(title, MAX_TITLE) or "(untitled)", "snippet": _text(snippet, MAX_SNIPPET), "url": url[:MAX_URL]}


def _used_today(ctx: AppContext) -> int:
    midnight = datetime.combine(ctx.now().date(), time(), ctx.tz).astimezone(timezone.utc)
    return ctx.db.execute("SELECT COUNT(*) FROM web_searches WHERE at >= ?",
                          (midnight.isoformat(timespec="seconds"),)).fetchone()[0]


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    return _request(req)


def _request(req: urllib.request.Request) -> dict:
    try:
        with _opener().open(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read(2 * 1024 * 1024))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise ActionError("Search isn't working: the search API key was refused.") from None
        if e.code == 429:
            raise ActionError("Search isn't working right now: the search service's rate limit or quota is used up.") from None
        raise ActionError(f"Search isn't working: the search service answered {e.code}.") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ActionError("I couldn't search: the search service can't be reached (no network?).") from None
    except ValueError:
        raise ActionError("Search isn't working: the search service sent back something unreadable.") from None


def search(ctx: AppContext, query: str, n: int = 5) -> Result:
    q = clean_query(query)
    if not q:
        raise ActionError("There was nothing left to search for once links and email addresses were taken out.")
    key, provider = ctx.secrets.get("SEARCH_API_KEY"), str(ctx.settings["provider"])
    if not key:
        raise ActionError("Search isn't set up: add SEARCH_API_KEY to the .env file.")
    if provider == "google" and not ctx.secrets.get("SEARCH_ENGINE_ID"):
        raise ActionError("Google search needs SEARCH_ENGINE_ID in the .env file too.")
    limit = int(ctx.settings["daily_limit"])
    if _used_today(ctx) >= limit:
        raise ActionError(f"Today's {limit} searches are used up, so I can't search again until tomorrow.")
    ctx.db.execute("INSERT INTO web_searches (at) VALUES (?)",
                   (datetime.now(timezone.utc).isoformat(timespec="seconds"),))
    if provider == "brave":
        req = urllib.request.Request(f"{BRAVE}?{urllib.parse.urlencode({'q': q, 'count': n})}",
                                     headers={"Accept": "application/json", "X-Subscription-Token": key})
        data = _request(req)
        raw = [(i.get("title"), i.get("description"), i.get("url"))
               for i in (data.get("web") or {}).get("results") or [] if isinstance(i, dict)]
    else:
        params = {"key": key, "cx": ctx.secrets["SEARCH_ENGINE_ID"], "q": q, "num": n}
        data = _get_json(f"{GOOGLE}?{urllib.parse.urlencode(params)}")
        raw = [(i.get("title"), i.get("snippet"), i.get("link")) for i in data.get("items") or [] if isinstance(i, dict)]
    results = [r for r in (_result(*t) for t in raw) if r][:n]
    if not results:
        return Result(f"The search for '{q}' found nothing.", data={"query": q, "results": []})
    lines = [f"{i}. {r['title']}\n   {r['snippet']}\n   {r['url']}" for i, r in enumerate(results, 1)]
    return Result(f"Results for '{q}':\n" + "\n".join(lines), data={"query": q, "results": results})


def read_page(ctx: AppContext, url: str) -> Result:
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    try:
        got = page.fetch(url, lambda u: _check(ctx, u), lambda req: _page_opener(ctx)(req, timeout=page.TIMEOUT))
    except page.Refused as e:
        raise ActionError(f"I can't read that page: {e}.") from None
    if got.thin:
        return Result(f"{got.url}: the page needs a browser to show its text, and I read pages without one. "
                      "All I got was:\n" + (got.text or "(nothing)"), data={"url": got.url, "thin": True})
    links = "\n".join(f"- {t or '(no text)'}: {h}" for t, h in got.links)
    cut = " (cut short)" if len(got.text) >= page.MAX_TEXT else ""
    text = f"{got.title or got.url}\n{got.url}\n\nText{cut}:\n{got.text or '(none)'}\n\nLinks:\n{links or '(none)'}"
    return Result(text, data={"url": got.url, "title": got.title, "links": [list(x) for x in got.links]})
