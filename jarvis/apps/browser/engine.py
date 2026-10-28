"""The browser behind a small interface: the manager and the actions only ever see `Driver`, `Session` and `PageDriver`,
so the tests use a fake and `uv run pytest` needs no Chromium. `PlaywrightDriver` is the real one.

Playwright's sync API belongs to the thread that started it. The manager runs every call on one worker thread."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from jarvis.apps.browser.steps import Control, Step

SCREEN = {"width": 1280, "height": 800}
JPEG_QUALITY = 70
Check = Callable[[str], "str | None"]  # url -> why it must not be requested, or None


class BrowserUnavailable(Exception):
    """Playwright or its Chromium isn't installed; the message says what to run."""


class PageFailed(Exception):
    """The page didn't load; the message is one plain sentence."""


@dataclass
class Loaded:
    url: str
    title: str
    text: str
    links: list[tuple[str, str]]
    status: int | None = None


class PageDriver(Protocol):
    url: str

    def goto(self, url: str, load_seconds: int) -> int | None: ...
    def read(self) -> Loaded: ...
    def screenshot(self) -> bytes: ...
    def controls(self) -> list[Control]: ...
    def run(self, steps: list[tuple[Step, Control]]) -> None: ...
    def follow(self, link_text: str, load_seconds: int) -> None: ...
    def close(self) -> None: ...


class Session(Protocol):
    headed: bool

    def new_page(self) -> PageDriver: ...
    def alive(self) -> bool: ...
    def get_json(self, url: str, check: Check) -> tuple[int, dict | list | None, str | None, str]: ...
    def close(self) -> None: ...


class Driver(Protocol):
    def launch(self, profile: Path | None, headed: bool, check: Check, browsers: Path | None) -> Session: ...


# ---- the real one -----------------------------------------------------------------------------------------------

CONTROLS_JS = """() => {
  const out = []; let n = 0;
  const shown = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const words = t => (t || '').replace(/\\s+/g, ' ').trim();
  const label = e => {
    const parts = [];
    if (e.labels) for (const l of e.labels) parts.push(words(l.innerText));
    parts.push(words(e.getAttribute('aria-label')));
    const by = e.getAttribute('aria-labelledby');
    if (by) for (const id of by.split(/\\s+/)) { const t = document.getElementById(id); if (t) parts.push(words(t.innerText)); }
    const tag = e.tagName.toLowerCase();
    if (tag === 'button' || e.getAttribute('role') === 'button') parts.push(words(e.innerText));
    if (tag === 'input' && ['submit', 'button', 'reset'].includes(e.type)) parts.push(words(e.value));
    parts.push(words(e.getAttribute('placeholder')), words(e.title));
    return parts.find(p => p) || '';
  };
  for (const e of document.querySelectorAll('input, textarea, select, button, [role=button]')) {
    if (!shown(e)) continue;
    const tag = e.tagName.toLowerCase(), type = (e.type || 'text').toLowerCase();
    let kind = 'text';
    if (tag === 'select') kind = 'select';
    else if (tag === 'button' || e.getAttribute('role') === 'button' || ['submit', 'button', 'reset', 'image'].includes(type)) kind = 'button';
    else if (tag === 'input' && ['checkbox', 'radio', 'password', 'file'].includes(type)) kind = type;
    else if (tag === 'input' && type === 'hidden') continue;
    e.setAttribute('data-falak-i', String(n));
    out.push({i: n, kind, label: label(e), autocomplete: (e.getAttribute('autocomplete') || '').toLowerCase(),
              name: e.name || '', disabled: !!e.disabled,
              options: tag === 'select' ? Array.from(e.options).map(o => words(o.label || o.text)) : []});
    n++;
  }
  return out;
}"""

READ_JS = """() => ({
  text: document.body ? document.body.innerText : '',
  links: Array.from(document.querySelectorAll('a[href]')).map(a => [(a.innerText || a.title || '').trim().slice(0, 80), a.href])
    .filter(([t, h]) => h.startsWith('http'))
})"""


def _first_line(e: Exception) -> str:
    return (str(e).strip().splitlines() or ["unknown error"])[0][:200]


class _PwPage:
    def __init__(self, page, owner, guard: Check, fresh_context=None) -> None:
        self._p, self._owner, self._fresh = page, owner, fresh_context
        self.guard = guard

    @property
    def url(self) -> str:
        return self._p.url

    def goto(self, url: str, load_seconds: int) -> int | None:
        from playwright.sync_api import Error, TimeoutError as PwTimeout
        status = None
        try:
            response = self._p.goto(url, wait_until="domcontentloaded", timeout=load_seconds * 1000)
            status = response.status if response else None
            try:  # settle: scripts have usually drawn the page once the network goes quiet; a busy page never does
                self._p.wait_for_load_state("networkidle", timeout=load_seconds * 1000)
            except PwTimeout:
                pass
        except PwTimeout:
            pass  # use what loaded
        except Error as e:
            raise PageFailed(_first_line(e)) from None
        return status

    def read(self) -> Loaded:
        got = self._p.evaluate(READ_JS)
        return Loaded(self._p.url, self._p.title(), got["text"], [(t, h) for t, h in got["links"]])

    def screenshot(self) -> bytes:
        return self._p.screenshot(type="jpeg", quality=JPEG_QUALITY)

    def controls(self) -> list[Control]:
        return [Control(c["i"], c["kind"], c["label"], tuple(c["options"]), c["autocomplete"], c["name"], c["disabled"])
                for c in self._p.evaluate(CONTROLS_JS)]

    def run(self, steps: list[tuple[Step, Control]]) -> None:
        from playwright.sync_api import Error
        try:
            for step, control in steps:
                where = self._p.locator(f'[data-falak-i="{control.index}"]')
                if step.verb == "fill":
                    where.fill(step.value, timeout=5000)
                elif step.verb == "select":
                    where.select_option(label=step.value, timeout=5000)
                elif step.verb == "check":
                    where.set_checked(True, timeout=5000)
                else:
                    where.click(timeout=5000)
                    try:
                        self._p.wait_for_load_state("networkidle", timeout=5000)
                    except Exception:
                        pass
        except Error as e:
            raise PageFailed(_first_line(e)) from None

    def follow(self, link_text: str, load_seconds: int) -> None:
        from playwright.sync_api import Error
        links = self._p.locator("a[href]").filter(has_text=link_text)
        want = " ".join(link_text.split()).lower()
        exact = [i for i in range(links.count()) if " ".join(links.nth(i).inner_text().split()).lower() == want]
        if len(exact) != 1:
            raise PageFailed(f"“{link_text}” matches {len(exact)} links on the page" if exact else
                             f"the page has no link called “{link_text}”")
        href = links.nth(exact[0]).evaluate("a => a.href")
        if (why := self.guard(href)):
            raise PageFailed(why)
        try:
            links.nth(exact[0]).click(timeout=5000)
        except Error as e:
            raise PageFailed(_first_line(e)) from None
        try:
            self._p.wait_for_load_state("networkidle", timeout=load_seconds * 1000)
        except Exception:
            pass

    def close(self) -> None:
        for thing in (self._p, self._fresh):
            try:
                if thing is not None:
                    thing.close()
            except Exception:
                pass


class _PwSession:
    def __init__(self, pw, context, browser, check: Check, headed: bool) -> None:
        self._pw, self._context, self._browser, self._check, self.headed = pw, context, browser, check, headed

    def _guarded(self, context):
        def route(r) -> None:
            if self._check(r.request.url):
                r.abort("blockedbyclient")
            else:
                r.continue_()

        context.route("**/*", route)
        return context

    def new_page(self) -> _PwPage:
        if self._browser is None:  # the persistent profile: tabs of one context
            return _PwPage(self._context.new_page(), self, self._check)
        fresh = self._guarded(self._browser.new_context(accept_downloads=False, viewport=SCREEN,
                                                         service_workers="block"))
        return _PwPage(fresh.new_page(), self, self._check, fresh)

    def alive(self) -> bool:
        try:
            if self._browser is not None:
                return self._browser.is_connected() and (not self.headed or bool(self._browser.contexts and any(
                    c.pages for c in self._browser.contexts)))
            return bool(self._context.pages) or not self.headed
        except Exception:
            return False

    def get_json(self, url: str, check: Check) -> tuple[int, dict | list | None, str | None, str]:
        """A GET with the profile's cookies. Redirects are followed by hand so each hop is checked. Returns
        (status, json or None, the Link header's next page, the final URL)."""
        import json as jsonlib
        import re
        hops = 0
        while True:
            reply = self._context.request.get(url, max_redirects=0, fail_on_status_code=False,
                                              headers={"Accept": "application/json"}, timeout=20000)
            if reply.status in (301, 302, 303, 307, 308) and (to := reply.headers.get("location")) and hops < 5:
                from urllib.parse import urljoin
                url, hops = urljoin(url, to), hops + 1
                if (why := check(url)):
                    raise PageFailed(why)
                if hops and self._signed_out_hint(url):
                    return reply.status, None, None, url
                continue
            body = reply.body()[:8 * 1024 * 1024].decode("utf-8", "replace")
            body = re.sub(r"^while\(1\);", "", body.lstrip())  # Canvas's guard against script inclusion
            try:
                data = jsonlib.loads(body) if reply.status < 300 else None
            except ValueError:
                data = None
            nxt = re.search(r'<([^>]+)>\s*;\s*rel="next"', reply.headers.get("link", ""))
            return reply.status, data, nxt[1] if nxt else None, url

    @staticmethod
    def _signed_out_hint(url: str) -> bool:
        from jarvis.apps.browser.sites import looks_like_sign_in
        return looks_like_sign_in(url)

    def close(self) -> None:
        for thing in (self._context if self._browser is None else None, self._browser):
            try:
                if thing is not None:
                    thing.close()
            except Exception:
                pass
        try:
            self._pw.stop()
        except Exception:
            pass


class PlaywrightDriver:
    def launch(self, profile: Path | None, headed: bool, check: Check, browsers: Path | None) -> _PwSession:
        if browsers is not None:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers)  # before the driver starts, which reads it
        try:
            from playwright.sync_api import Error, sync_playwright
        except ImportError:
            raise BrowserUnavailable("Falak's browser isn't installed. Run: uv sync --extra browser, then falak "
                                     "browser setup.") from None
        pw = sync_playwright().start()
        try:
            if profile is not None:
                window = {"no_viewport": True} if headed else {"viewport": SCREEN}  # a window fills itself
                context = pw.chromium.launch_persistent_context(
                    str(profile), headless=not headed, accept_downloads=False, service_workers="block", **window)
                session = _PwSession(pw, context, None, check, headed)
                session._guarded(context)
            else:
                browser = pw.chromium.launch(headless=not headed)
                session = _PwSession(pw, None, browser, check, headed)
            return session
        except Error as e:
            pw.stop()
            if "Executable doesn't exist" in str(e) or "playwright install" in str(e):
                raise BrowserUnavailable("Falak's browser (Chromium) isn't installed yet. Run: falak browser "
                                         "setup") from None
            raise PageFailed(_first_line(e)) from None
