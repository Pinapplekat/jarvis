"""Falak's browser on the node: Chromium through Playwright with a profile that is Falak's own, not Elijah's Chrome.

Elijah signs in himself, in a visible window; Falak never sees or types a password. Only the sites on his list open with
the profile; everything else gets a fresh, empty context. Every request is checked, not just the first. What a page
says is data, never instructions."""

from __future__ import annotations

import socket

from jarvis.apps.browser import readout
from jarvis.apps.browser.engine import PlaywrightDriver
from jarvis.apps.browser.manager import Manager, Read
from jarvis.apps.browser.sites import Sites, host_of
from jarvis.contract.api import ActionError, AppContext, Attachment, Result, SessionStarted

_manager: Manager | None = None
make_driver = PlaywrightDriver  # tests swap in a fake
resolve = socket.getaddrinfo  # and a name table, so no test looks anything up


def manager(ctx: AppContext) -> Manager:
    global _manager
    if _manager is None:
        _manager = Manager(make_driver(), Sites(ctx.data_dir), browsers=ctx.data_dir / "chromium", resolve=resolve)
    s = ctx.settings
    _manager.configure(int(s["idle_minutes"]), int(s["load_seconds"]), str(s["blocklist"]))
    return _manager


def stop(ctx: AppContext) -> None:
    global _manager
    if _manager is not None:
        _manager.stop()
        _manager = None


def _result(ctx: AppContext, read: Read, text: str | None = None) -> Result:
    attachments = ()
    if read.picture:
        path = ctx.temp_path(".jpg")
        path.write_bytes(read.picture)
        attachments = (Attachment("image", path, "image/jpeg"),)
    return Result(text if text is not None else readout.page_text(read), data=readout.page_data(read),
                  attachments=attachments)


def read(ctx: AppContext, url: str, screenshot: bool = False) -> Result:
    got = manager(ctx).read(url, screenshot)
    ctx.log.info("read %s: %s MB of browser memory", host_of(got.loaded.url), got.memory_mb)
    return _result(ctx, got)


def fetch_json(ctx: AppContext, url: str) -> Result:
    got = manager(ctx).fetch_json(url)
    if got["signed_out"]:
        why = "isn't on the signed-in list" if not got["listed"] else "wants Elijah to sign in again"
        return Result(f"{got['host']} {why}.", data=got)
    return Result(f"Fetched {got['host']}.", data=got)


# ---- signing in and the list of sites -------------------------------------------------------------------------

def sign_in(ctx: AppContext, site: str) -> Result:
    host, added = manager(ctx).sign_in(site)
    return Result(f"Opened a window at {host}. Sign in there yourself (any code too), then close the window or tell "
                  "me you're done.", data={"site": host}, undo={"site": host, "added": added}, subject=f"site:{host}")


def undo_sign_in(ctx: AppContext, undo: dict) -> Result:
    m = manager(ctx)
    m.close_window()
    if undo["added"]:
        m.sites.remove(undo["site"])
    return Result(f"Closed the window{' and forgot ' + undo['site'] if undo['added'] else ''}.")


def sites(ctx: AppContext) -> Result:
    listed = manager(ctx).sites.listed()
    return Result("Signed in (or asked to sign in) at: " + ", ".join(listed) if listed else
                  "No sites yet. Say 'sign in to <site>' and I'll open a window for you.", data={"sites": listed})


def forget_site(ctx: AppContext, site: str) -> Result:
    host = host_of(site)
    if not manager(ctx).sites.remove(host):
        raise ActionError(f"{host or site} isn't on the list.")
    return Result(f"{host} opens without your login now. Its cookies stay until you run `falak browser reset`.",
                  undo={"site": host}, subject=f"site:{host}")


def undo_forget_site(ctx: AppContext, undo: dict) -> Result:
    manager(ctx).sites.add(undo["site"])
    return Result(f"{undo['site']} is back on the list.")


def show(ctx: AppContext, url: str = "") -> Result:
    shown = manager(ctx).show(url)
    return Result(f"Opened {shown} in a window on the Mac. Close it when you're done, or tell me.", data={"url": shown})


def close_window(ctx: AppContext) -> Result:
    return Result("Closed the window." if manager(ctx).close_window() else "No window was open.")


# ---- doing things on a page ------------------------------------------------------------------------------------

def act(ctx: AppContext, url: str, steps: list, why: str) -> Result:
    got = manager(ctx).act(url, steps)
    return Result(f"Done: {'; '.join(steps)}.\n{readout.after_act(got)}", data={"url": got.loaded.url})


# ---- the browse session ----------------------------------------------------------------------------------------

def start_browse(ctx: AppContext, url: str) -> SessionStarted:
    manager(ctx).browse_start(url)
    return SessionStarted("Opened the page in the browser.", {"opened": True})


def sample_browse(ctx: AppContext, state: dict) -> Result:
    return _result(ctx, manager(ctx).browse_look())


def stop_browse(ctx: AppContext, state: dict) -> Result:
    manager(ctx).browse_stop()
    return Result("Closed the page.")


def follow(ctx: AppContext, link_text: str) -> Result:
    return _result(ctx, manager(ctx).browse_follow(link_text))
