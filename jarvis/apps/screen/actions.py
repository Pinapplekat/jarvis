import subprocess
from dataclasses import dataclass

from jarvis.contract.api import ActionError, AppContext, Attachment, Result, SessionStarted

SCREENCAPTURE = "/usr/sbin/screencapture"
SIPS = "/usr/bin/sips"
MAX_WIDTH = 1600
TIMEOUT = 15
SYSTEM_OWNERS = {"Window Server", "Dock", "Control Center", "SystemUIServer", "Notification Center",
                 "Spotlight", "loginwindow", "WindowManager"}
EXCLUDED_APPS = {"messages", "passwords", "keychain access", "1password", "1password 7", "bitwarden",
                 "lastpass", "dashlane", "keeper password manager", "enpass", "nordpass"}
EXCLUDED_TITLE_WORDS = ["bank", "chase", "wells fargo", "capital one", "credit union", "venmo", "paypal",
                        "zelle", "cash app", "robinhood", "coinbase", "schwab", "fidelity", "vanguard",
                        "discover card", "american express", "amex", "password", "passkey", "sign in to",
                        "log in to", "1password", "bitwarden"]
DIDNT_LOOK = "Your screen shows a banking or password app, so I didn't look."
NO_WINDOW = "I couldn't tell which window is in front, so I didn't look."
BLOCKED = ("Screen Recording is blocked. Allow it for the terminal app running me in "
           "System Settings > Privacy & Security > Screen Recording.")


@dataclass
class Window:
    app: str
    title: str | None  # None when Screen Recording permission is missing


def _words(text: str) -> list[str]:
    return [w.strip().lower() for w in text.split(",") if w.strip()]


def excluded(app: str, title: str | None, extra_apps: str = "", extra_titles: str = "") -> bool:
    """Whether this window must not be looked at."""
    if app.lower() in EXCLUDED_APPS | set(_words(extra_apps)):
        return True
    lowered = (title or "").lower()
    return any(w in lowered for w in EXCLUDED_TITLE_WORDS + _words(extra_titles))


def front_window() -> Window | None:
    """The frontmost app's top window (Quartz window list + NSWorkspace), or None when it can't be read."""
    import Quartz
    from AppKit import NSWorkspace

    front = NSWorkspace.sharedWorkspace().frontmostApplication()
    if front is None:
        return None
    pid = front.processIdentifier()
    options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    for w in Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []:
        if w.get("kCGWindowOwnerPID") != pid or w.get("kCGWindowLayer", 1) != 0:
            continue
        owner = w.get("kCGWindowOwnerName") or front.localizedName() or "?"
        if owner in SYSTEM_OWNERS:
            return None
        bounds = w.get("kCGWindowBounds") or {}
        if bounds.get("Width", 0) < 200 or bounds.get("Height", 0) < 120:
            continue  # skip tiny helper windows
        return Window(str(owner), w.get("kCGWindowName"))
    return None


def locked() -> bool:
    """True if the Mac's screen is locked (or at the login window). False when it can't tell."""
    try:
        import Quartz

        session = Quartz.CGSessionCopyCurrentDictionary() or {}
        return bool(session.get("CGSSessionScreenIsLocked")) or session.get("kCGSSessionOnConsoleKey") is False
    except Exception:
        return False


def _capture(ctx: AppContext) -> Result:
    if locked():
        raise ActionError("The screen is locked, so there is nothing to see.")
    window = front_window()
    if window is None:
        return Result(NO_WINDOW)
    if excluded(window.app, window.title, str(ctx.settings["exclude_apps"]), str(ctx.settings["exclude_titles"])):
        return Result(DIDNT_LOOK)  # an answer, not an error; no picture was taken
    path = ctx.temp_path(".jpg")
    try:
        r = subprocess.run([SCREENCAPTURE, "-x", "-t", "jpg", str(path)], capture_output=True, text=True, timeout=TIMEOUT)
        if r.returncode != 0 or path.stat().st_size == 0:
            raise ActionError(BLOCKED)
        subprocess.run([SIPS, "-Z", str(MAX_WIDTH), str(path)], capture_output=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        path.unlink(missing_ok=True)
        raise ActionError("The screen capture didn't finish in time.") from None
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return Result("Here is the screen.", attachments=(Attachment("image", path, "image/jpeg"),))


def look(ctx: AppContext) -> Result:
    return _capture(ctx)


def start_live(ctx: AppContext) -> SessionStarted:
    return SessionStarted("Watching the screen.", {})  # nothing runs in the background; each look captures


def sample_live(ctx: AppContext, state: dict) -> Result:
    return _capture(ctx)


def stop_live(ctx: AppContext, state: dict) -> Result:
    return Result("Closed the screen view.")
