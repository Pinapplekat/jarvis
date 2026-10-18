"""JARVIS's own browser, with a persistent profile so it can stay logged in to chosen sites.

Elijah logs in himself, in a visible window (`jarvis browser login <site>`); JARVIS never
asks for, sees or stores a password. The profile (cookies, sessions) lives in
Application Support/JARVIS/browser-profile on this Mac: outside the repo, unreadable to
commands and tasks (it's in the secrets guard), and never sent to Telegram. Only sites on his
list are opened with the profile; anything else gets a fresh, empty context. Banking and
payment sites are blocked outright. Every request is re-checked, so a page can't point the
browser at this Mac or the local network. Page text is untrusted data.
"""

import fcntl
import json
import time
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlparse

from jarvis import config
from jarvis.web import MAX_LINKS, MAX_TEXT, Page, _clean, blocked_site, check_url

SITES = config.DATA_DIR / "browser-sites.json"
LOCK = config.DATA_DIR / "browser.lock"
SCREEN = {"width": 1280, "height": 900}


@dataclass
class Shot:
    page: Page
    png: bytes | None = None
    logged_in: bool = False  # opened with his saved profile
    path: str = ""           # where the screenshot was saved (attachable later)


def _host(url: str) -> str:
    host = (urlparse(url if "//" in url else f"https://{url}").hostname or "").lower().rstrip(".")
    return host.removeprefix("www.")


def sites() -> list[str]:
    try:
        return json.loads(SITES.read_text())
    except (OSError, ValueError):
        return []


def _save_sites(names: list[str]) -> None:
    SITES.parent.mkdir(parents=True, exist_ok=True)
    SITES.write_text(json.dumps(sorted(set(names))))


def allowed(url: str) -> bool:
    host = _host(url)
    return any(host == s or host.endswith("." + s) for s in sites())


def forget_site(site: str) -> bool:
    """Take a site off the list. (Its cookies stay in the profile until `jarvis browser reset`.)"""
    names = sites()
    if _host(site) not in names:
        return False
    _save_sites([s for s in names if s != _host(site)])
    return True


@contextmanager
def _locked(wait: float = 20):
    """One process at a time may use the profile (Chromium locks it anyway, less politely)."""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("w") as f:
        deadline = time.time() + wait
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() > deadline:
                    raise RuntimeError("the browser is busy with another page; try again in a moment") from None
                time.sleep(0.5)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _guard(context) -> None:
    context.route("**/*", lambda route: route.abort()
                  if check_url(route.request.url) or blocked_site(route.request.url) else route.continue_())


def login(site: str, timeout_minutes: int = 15) -> str:
    """Open a visible window on `site` for Elijah to sign in himself; when he closes it, the
    site goes on the list. Run from the terminal at the Mac, never from chat."""
    url = site if "//" in site else f"https://{site}"
    if (why := blocked_site(url) or check_url(url)):
        raise RuntimeError(why)
    from playwright.sync_api import sync_playwright
    config.BROWSER_PROFILE.mkdir(parents=True, exist_ok=True)
    config.BROWSER_PROFILE.chmod(0o700)
    with _locked(), sync_playwright() as p:
        context = p.chromium.launch_persistent_context(str(config.BROWSER_PROFILE), headless=False,
                                                       accept_downloads=False, viewport=SCREEN)
        _guard(context)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(url)
        closed = {"done": False}
        context.on("close", lambda *_: closed.update(done=True))
        deadline = time.time() + timeout_minutes * 60
        while not closed["done"] and context.pages and time.time() < deadline:
            time.sleep(0.5)
        try:
            context.close()
        except Exception:
            pass
    _save_sites([*sites(), _host(url)])
    return _host(url)


def browse(url: str, screenshot: bool = False, settle_ms: int = 2500) -> Shot:
    """Open a page: with his saved logins if the site is on his list, otherwise in a fresh
    context. Returns its text and links, and a PNG of the viewport if asked."""
    if (why := blocked_site(url) or check_url(url)):
        return Shot(Page(url, error=why))
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright
    use_profile = allowed(url) and config.BROWSER_PROFILE.exists()
    try:
        with _locked(), sync_playwright() as p:
            if use_profile:
                context = p.chromium.launch_persistent_context(str(config.BROWSER_PROFILE), headless=True,
                                                               accept_downloads=False, viewport=SCREEN)
                browser = None
            else:
                browser = p.chromium.launch()
                context = browser.new_context(accept_downloads=False, viewport=SCREEN)
            try:
                _guard(context)
                page = context.new_page()
                try:
                    page.goto(url, wait_until="networkidle", timeout=15_000)
                except PlaywrightError:
                    page.wait_for_timeout(1000)  # busy pages never go idle; use what loaded
                page.wait_for_timeout(settle_ms)
                text = page.evaluate("() => document.body ? document.body.innerText : ''")
                links = page.evaluate(
                    """() => Array.from(document.querySelectorAll('a[href]'))
                         .map(a => [a.innerText.trim().slice(0, 80), a.href])
                         .filter(([t, h]) => h.startsWith('http'))""")
                png = page.screenshot(type="png") if screenshot else None
                result = Page(url=page.url, title=page.title(), text=_clean(text)[:MAX_TEXT],
                              links=list(dict.fromkeys(map(tuple, links)))[:MAX_LINKS])
                if (why := blocked_site(page.url)):  # redirected somewhere off limits
                    return Shot(Page(url, error=why))
                from jarvis.attachments import save_shot
                saved = str(save_shot(png, _host(page.url) or "page")) if png else ""
                return Shot(result, png, logged_in=use_profile, path=saved)
            finally:
                context.close()
                if browser:
                    browser.close()
    except (PlaywrightError, RuntimeError) as e:
        return Shot(Page(url, error=f"couldn't load the page: {str(e).splitlines()[0][:200]}"))


def reset() -> None:
    """Delete the profile (every saved login) and the site list."""
    import shutil
    with _locked():
        shutil.rmtree(config.BROWSER_PROFILE, ignore_errors=True)
    SITES.unlink(missing_ok=True)
