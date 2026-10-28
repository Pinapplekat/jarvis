"""One browser at a time: which kind of context a page needs, when it closes, and what each action does with a page.

Kinds: the signed-in profile (for sites on Elijah's list), a fresh empty context (everything else), and a visible
window for either. Everything runs on one worker thread, because Playwright's sync API belongs to the thread that
started it. The browser closes after `idle_minutes` without use, and a window after WINDOW_MINUTES."""

from __future__ import annotations

import os
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from jarvis.apps.browser.engine import BrowserUnavailable, Driver, Loaded, PageDriver, PageFailed, Session
from jarvis.apps.browser.guard import Guard
from jarvis.apps.browser.sites import Sites, host_of, looks_like_sign_in
from jarvis.apps.browser.steps import Control, Step, parse, resolve
from jarvis.contract.api import ActionError

WINDOW_MINUTES = 10
SWEEP_SECONDS = 10
PUMP_MS = 100  # how long each wait inside the browser is while a window is open


@dataclass
class Read:
    loaded: Loaded
    picture: bytes | None
    signed_in: bool  # the site is on Elijah's list, so the page was opened with his profile
    memory_mb: float | None


@dataclass
class Browse:
    page: PageDriver | None
    url: str  # where it was, to reopen it after a window took the browser


def normalize(url: str) -> str:
    url = url.strip()
    return url if "://" in url else "https://" + url


def rss_mb() -> float | None:
    """Memory of everything this process started (the browser and its helpers), from `ps`."""
    try:
        rows = subprocess.run(["/bin/ps", "-axo", "pid=,ppid=,rss="], capture_output=True, text=True, timeout=5).stdout
        table = {int(p): (int(pp), int(r)) for p, pp, r in (line.split() for line in rows.splitlines() if line.strip())}
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    mine, total = {os.getpid()}, 0
    grew = True  # children can have lower pids than their parents, so loop until nothing new joins
    while grew:
        grew = False
        for pid, (parent, rss) in table.items():
            if parent in mine and pid not in mine:
                mine.add(pid)
                total += rss
                grew = True
    return round(total / 1024, 1)


class Manager:
    def __init__(self, driver: Driver, sites: Sites, *, idle_minutes: int = 5, load_seconds: int = 15,
                 blocklist: str = "", browsers: Path | None = None, clock: Callable[[], float] = time.monotonic,
                 autosweep: bool = True, resolve=socket.getaddrinfo) -> None:
        self.driver, self.sites, self.browsers, self.clock = driver, sites, browsers, clock
        self.idle_minutes, self.load_seconds = idle_minutes, load_seconds
        self.guard = Guard(blocklist, resolve)
        self._pool = ThreadPoolExecutor(1, thread_name_prefix="falak-browser")
        self._autosweep, self._stopped = autosweep, threading.Event()
        self._yield = threading.Event()  # set when a call wants the browser's thread back from the pump
        self._monitor: threading.Thread | None = None
        # touched only on the worker thread
        self._session: Session | None = None
        self._kind: tuple[bool, bool] = (False, False)  # (signed-in profile, visible window)
        self._used = 0.0
        self._window_until = 0.0
        self._browse: Browse | None = None
        self._last_url = ""

    def configure(self, idle_minutes: int, load_seconds: int, blocklist: str) -> None:
        self.idle_minutes, self.load_seconds, self.guard.extra = idle_minutes, load_seconds, blocklist

    def run(self, fn, *args):
        """Run `fn` on the browser's one thread. A pump keeping an open window responsive gives way first, and
        starts again after."""
        self._yield.set()
        try:
            return self._pool.submit(fn, *args).result()
        finally:
            self._resume_pump()

    def _resume_pump(self) -> None:
        if self._session is not None and self._kind[1] and not self._stopped.is_set():
            self._yield.clear()
            self._pool.submit(self._pump)

    def _pump(self) -> None:
        """While a window is open, keep the browser's events running so each request's check (and so the page) never
        waits on Falak's next call. Without this the sign-in window hung on every click."""
        while not self._yield.is_set() and self._session is not None and self._kind[1]:
            try:
                self._session.pump(PUMP_MS)
            except Exception:
                return

    # ---- the browser itself ---------------------------------------------------------------------------------

    def _close_session(self) -> None:
        session, self._session = self._session, None
        if self._browse is not None:
            self._browse.page = None
        if session is not None:
            try:
                session.close()
            except Exception:
                pass

    def _acquire(self, url: str, window: bool = False) -> tuple[Session, bool]:
        """The session a page at `url` needs, launching or switching if it must. Returns (session, with_profile)."""
        if self._session is not None and not self._session.alive():
            self._close_session()
        profile = self.sites.covers(url) is not None
        want = (profile, window)
        if self._session is not None and self._kind != want:
            if self._kind[1] and not window:
                raise ActionError("A browser window is open for you. Finish there, or tell me you're done and I'll "
                                  "close it.")
            self._close_session()
        if self._session is None:
            try:
                self._session = self.driver.launch(self.sites.make_profile() if profile else None, window,
                                                   self.guard.check, self.browsers)
            except BrowserUnavailable as e:
                raise ActionError(str(e)) from None
            except PageFailed as e:
                raise ActionError(f"The browser wouldn't start ({e}).") from None
            self._kind = want
            self._window_until = self.clock() + WINDOW_MINUTES * 60
            self._start_monitor()
        self._used = self.clock()
        return self._session, profile

    def _start_monitor(self) -> None:
        if self._autosweep and self._monitor is None:
            self._monitor = threading.Thread(target=self._watch, name="falak-browser-idle", daemon=True)
            self._monitor.start()

    def _watch(self) -> None:
        while not self._stopped.wait(SWEEP_SECONDS):
            try:
                self.run(self.sweep)
            except Exception:
                pass

    def sweep(self) -> bool:
        """Close what has sat unused too long, and a window Elijah closed or left open. True if it closed the browser."""
        if self._session is None:
            return False
        now = self.clock()
        if self._kind[1]:
            gone = not self._session.alive() or now >= self._window_until
        else:
            gone = now - self._used >= self.idle_minutes * 60
        if gone:
            self._close_session()
        return gone

    def open(self) -> bool:
        return self._session is not None and self._session.alive()

    def stop(self) -> None:
        if self._stopped.is_set():
            return
        self._stopped.set()
        try:
            self.run(self._close_session)
        finally:
            self._pool.shutdown(wait=False)

    # ---- pages -----------------------------------------------------------------------------------------------

    def _load(self, page: PageDriver, url: str) -> None:
        self.guard.new_page()
        if (why := self.guard.check(url)):
            page.close()
            raise ActionError(f"I can't open that page: {why}.")
        try:
            page.goto(url, self.load_seconds)
        except PageFailed as e:
            page.close()
            blocked = self.guard.refused[0] if self.guard.refused else None
            raise ActionError(f"I can't open that page: {blocked}." if blocked else
                              f"I couldn't load that page ({e}).") from None
        self._last_url = page.url or url

    def _snapshot(self, page: PageDriver, picture: bool, signed_in: bool) -> Read:
        got = page.read()
        return Read(got, page.screenshot() if picture else None, signed_in, rss_mb())

    def read(self, url: str, picture: bool = False) -> Read:
        return self.run(self._read, normalize(url), picture)

    def _read(self, url: str, picture: bool) -> Read:
        if (why := self.guard.check(url)):  # before a browser is started for it
            raise ActionError(f"I can't open that page: {why}.")
        session, profile = self._acquire(url)
        page = session.new_page()
        self._load(page, url)
        try:
            return self._snapshot(page, picture, profile)
        finally:
            page.close()

    def fetch_json(self, url: str) -> dict:
        return self.run(self._fetch_json, normalize(url))

    def _fetch_json(self, url: str) -> dict:
        host = host_of(url)
        if self.sites.covers(url) is None:
            return {"signed_out": True, "listed": False, "host": host}
        self.guard.new_page()
        if (why := self.guard.check(url)):
            raise ActionError(f"I can't fetch that: {why}.")
        session, _ = self._acquire(url)
        try:
            status, data, nxt, final = session.get_json(url, self.guard.check)
        except PageFailed as e:
            raise ActionError(f"I couldn't fetch that ({e}).") from None
        out = {"status": status, "next": nxt, "host": host}
        if status == 401 or looks_like_sign_in(final) or (data is None and status < 400):
            return out | {"signed_out": True, "listed": True}
        if status >= 400:
            raise ActionError(f"{host} answered {status}.")
        return out | {"signed_out": False, "listed": True, "json": data}

    # ---- looking around (the browse session) -----------------------------------------------------------------

    def browse_start(self, url: str) -> None:
        self.run(self._browse_start, normalize(url))

    def _browse_start(self, url: str) -> None:
        if self._browse is not None and self._browse.page is not None:
            self._browse.page.close()
        session, _ = self._acquire(url)
        page = session.new_page()
        self._load(page, url)
        self._browse = Browse(page, page.url)

    def _browse_page(self) -> tuple[PageDriver, bool]:
        if self._browse is None:
            raise ActionError("No browse session is open. Start one with a page address.")
        profile = self.sites.covers(self._browse.url) is not None
        if self._browse.page is None or self._session is None or not self._session.alive():
            session, profile = self._acquire(self._browse.url)  # the browser closed (idle, or a window took it)
            self._browse.page = session.new_page()
            self._load(self._browse.page, self._browse.url)
        self._used = self.clock()
        return self._browse.page, profile

    def browse_look(self) -> Read:
        return self.run(self._browse_look)

    def _browse_look(self) -> Read:
        page, profile = self._browse_page()
        self._browse.url = page.url or self._browse.url
        return self._snapshot(page, True, profile)

    def browse_follow(self, link_text: str) -> Read:
        return self.run(self._browse_follow, link_text)

    def _browse_follow(self, link_text: str) -> Read:
        page, profile = self._browse_page()
        self.guard.new_page()
        try:
            page.follow(link_text, self.load_seconds)
        except PageFailed as e:
            blocked = self.guard.refused[0] if self.guard.refused else None
            raise ActionError(f"I can't follow that link: {blocked or e}.") from None
        self._browse.url = self._last_url = page.url
        return self._snapshot(page, True, profile)

    def browse_stop(self) -> None:
        self.run(self._browse_stop)

    def _browse_stop(self) -> None:
        if self._browse is not None and self._browse.page is not None:
            self._browse.page.close()
        self._browse = None

    # ---- acting ----------------------------------------------------------------------------------------------

    def plan(self, url: str, steps: list[str]) -> tuple[list[tuple[Step, Control]], PageDriver, bool]:
        """Open the page and check every step against it. Nothing has been done to the page when this returns."""
        parsed = parse(steps)
        session, profile = self._acquire(url)
        page = session.new_page()
        self._load(page, url)
        try:
            return resolve(parsed, page.controls()), page, profile
        except BaseException:
            page.close()
            raise

    def act(self, url: str, steps: list[str]) -> Read:
        return self.run(self._act, normalize(url), steps)

    def _act(self, url: str, steps: list[str]) -> Read:
        matched, page, profile = self.plan(url, steps)
        try:
            try:
                page.run(matched)
            except PageFailed as e:
                raise ActionError(f"The page stopped me partway ({e}). Some steps may have run; look at the page "
                                  "before trying again.") from None
            return self._snapshot(page, False, profile)
        finally:
            page.close()

    # ---- windows ---------------------------------------------------------------------------------------------

    def show(self, url: str = "") -> str:
        return self.run(self._show, normalize(url) if url.strip() else "")

    def _show(self, url: str) -> str:
        url = url or (self._browse.url if self._browse else "") or self._last_url
        if not url:
            raise ActionError("I haven't opened a page yet, so there is nothing to show. Give me an address.")
        if (why := self.guard.check(url)):
            raise ActionError(f"I can't open that page: {why}.")
        if self._session is not None and self._session.alive() and self._kind == (self.sites.covers(url) is not None, True):
            session = self._session  # a window of the right kind is already open: another tab in it
        else:
            self._close_session()  # a headless context can't be made visible: reopen the profile with a window
            session, _ = self._acquire(url, window=True)
        page = session.new_page()
        self._load(page, url)
        self._window_until = self.clock() + WINDOW_MINUTES * 60
        return page.url or url

    def sign_in(self, site: str) -> tuple[str, bool]:
        return self.run(self._sign_in, site)

    def _sign_in(self, site: str) -> tuple[str, bool]:
        host = host_of(site)
        url = f"https://{host}/"
        if not host or "." not in host:
            raise ActionError(f"“{site}” doesn't look like a website address.")
        if (why := self.guard.check(url)):
            raise ActionError(f"I can't open that site: {why}.")
        added = self.sites.add(host)  # listed first, so _acquire opens the profile
        try:
            self._close_session()
            session, _ = self._acquire(url, window=True)
            page = session.new_page()
            self._load(page, url)
        except BaseException:
            if added:
                self.sites.remove(host)
            raise
        return host, added

    def close_window(self) -> bool:
        return self.run(self._close_window)

    def _close_window(self) -> bool:
        if self._session is not None and self._kind[1]:
            self._close_session()
            return True
        return False
