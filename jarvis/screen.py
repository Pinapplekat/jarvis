"""Screen sense: read the text of the front window, on-device.

Captures only the frontmost window, runs Apple's Vision text recognition locally, and
deletes the image immediately; only (redacted) text is kept. Banking, password managers
and Messages are never captured. Needs macOS Screen Recording permission for the Python
that runs the background job.
"""

import re
import subprocess
import uuid
from dataclasses import dataclass
from difflib import SequenceMatcher

from jarvis import config

SYSTEM_OWNERS = {"Window Server", "Dock", "Control Center", "SystemUIServer", "Notification Center",
                 "Spotlight", "loginwindow", "WindowManager"}
EXCLUDED_APPS = {"messages", "passwords", "keychain access", "1password", "1password 7", "bitwarden",
                 "lastpass", "dashlane", "keeper password manager", "enpass", "nordpass"}
EXCLUDED_TITLE_WORDS = ["bank", "chase", "wells fargo", "capital one", "credit union", "venmo", "paypal",
                        "zelle", "cash app", "robinhood", "coinbase", "schwab", "fidelity", "vanguard",
                        "discover card", "american express", "amex", "password", "passkey", "sign in to",
                        "log in to", "1password", "bitwarden"]

SECRET_ASSIGN = re.compile(
    r"(?i)\b(?P<name>[\w.-]*(?:token|secret|passw(?:or)?d|passwd|api[_-]?key|apikey|auth|credential"
    r"|private[_-]?key|_key|_pat)[\w.-]*)(?:\s*[=:]\s*|\s+)(?P<q>['\"]?)[^\s'\"]{4,}(?P=q)"
)
LONG_RANDOM = re.compile(r"\b(?=[A-Za-z0-9_\-+/=:]*\d)(?=[A-Za-z0-9_\-+/=:]*[A-Za-z])[A-Za-z0-9_\-+/=:]{28,}\b")


@dataclass
class Window:
    window_id: int
    app: str
    title: str | None  # None when Screen Recording permission is missing


def redact(text: str) -> str:
    """Scrub things that look like secrets before anything is stored or sent."""
    text = SECRET_ASSIGN.sub(lambda m: f"{m['name']}=<redacted>", text)
    return LONG_RANDOM.sub("<redacted>", text)


def _list(name: str) -> list[str]:
    return [x.strip().lower() for x in name.split(",") if x.strip()]


def excluded(app: str, title: str | None) -> str | None:
    """Why this window must not be read, or None."""
    apps = EXCLUDED_APPS | set(_list(config.WATCH_EXCLUDE_APPS))
    if app.lower() in apps:
        return f"excluded app {app}"
    words = EXCLUDED_TITLE_WORDS + _list(config.WATCH_EXCLUDE_TITLES)
    lowered = (title or "").lower()
    if any(w in lowered for w in words):
        return "excluded window title"
    return None


def front_window() -> Window | None:
    """The frontmost app's top window (Quartz window list + NSWorkspace)."""
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
        return Window(int(w["kCGWindowNumber"]), str(owner), w.get("kCGWindowName"))
    return None


def ocr(path: str) -> str:
    import Vision
    from Foundation import NSURL

    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(path), None)
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(False)  # code and paths shouldn't be "corrected"
    try:  # the Neural Engine path fails on this Mac (e5rt errors, minutes per image); the CPU is quick
        request.setUsesCPUOnly_(True)
    except AttributeError:
        pass
    ok, error = handler.performRequests_error_([request], None)
    if not ok:
        raise RuntimeError(f"OCR failed: {error}")
    lines = []
    for result in request.results() or []:
        candidates = result.topCandidates_(1)
        if candidates:
            lines.append(str(candidates[0].string()))
    return "\n".join(lines)


def capture_text(window: Window) -> str:
    """Screenshot one window to a temp file, OCR it, delete the file. Returns redacted text."""
    tmp_dir = config.DATA_DIR / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    path = tmp_dir / f"{uuid.uuid4().hex}.png"
    try:
        r = subprocess.run(["screencapture", "-x", "-o", f"-l{window.window_id}", str(path)],
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0 or not path.exists():
            raise RuntimeError(f"screencapture failed: {r.stderr.strip()[:200] or r.returncode}")
        return redact(ocr(str(path)))[: config.WATCH_MAX_CHARS]
    finally:
        path.unlink(missing_ok=True)


def meaningfully_changed(old: str | None, new: str, threshold: float = 0.85) -> bool:
    if not old:
        return bool(new.strip())
    if old == new:
        return False
    return SequenceMatcher(None, old, new).quick_ratio() < threshold
