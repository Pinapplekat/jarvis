"""What JARVIS is doing right now, for the menu bar indicator.

Each part writes its own entry to one small JSON file:
  ears     - waiting (for "Hey Jarvis") | listening | working | speaking
  telegram - working (answering a message) | idle
  voice    - speaking (reminders, alerts, interjections)
  action   - acting (the one thing being done right now: a screenshot, a page, a reminder...),
             with the SF Symbol the menu bar and the HUD show for it (ACTIONS)
Busy states carry an expiry so a crash can never leave the indicator stuck; the ears entry
is refreshed as a heartbeat so a stopped listener shows as off.
"""

import fcntl
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager

from jarvis import config

PATH = config.DATA_DIR / "status.json"
BUSY_TTL = 120          # seconds a busy state may last without being refreshed
MIN_SHOW = 1.2          # an action stays on screen at least this long, so a quick one can still be seen
_lock = threading.Lock()

# What JARVIS is doing -> (SF Symbol, what the menu bar and HUD say). Keys are chat action types,
# plus a few steps that aren't actions (thinking, peek, inbox, location, ...). Shown in white.
ACTIONS: dict[str, tuple[str, str]] = {
    "thinking": ("brain.head.profile", "Thinking"),
    "screenshot": ("camera.viewfinder", "Taking a screenshot"),
    "look_at_screen": ("eye", "Looking at your screen"),
    "look_through_camera": ("camera", "Looking through the camera"),
    "peek": ("eye", "Glancing at your screen"),
    "open_url": ("globe", "Reading a web page"),
    "browse": ("globe", "Opening a web page"),
    "search": ("magnifyingglass.circle", "Searching the web"),
    "read_email": ("envelope.open", "Reading an email"),
    "inbox": ("tray.full", "Checking your inbox"),
    "draft_email": ("square.and.pencil", "Drafting an email"),
    "location": ("location.fill", "Checking your location"),
    "label_place": ("mappin.and.ellipse", "Naming a place"),
    "deadlines": ("calendar", "Checking your deadlines"),
    "add_deadline": ("calendar.badge.plus", "Adding a deadline"),
    "deadline_done": ("checkmark.circle", "Marking a deadline done"),
    "remind": ("bell", "Setting a reminder"),
    "cancel_reminder": ("bell.slash", "Cancelling a reminder"),
    "dont_remind": ("bell.slash", "Quieting a reminder"),
    "remember": ("bookmark", "Remembering that"),
    "forget": ("eraser", "Forgetting that"),
    "note": ("note.text", "Taking a note"),
    "project": ("folder", "Updating a project"),
    "plan_improvement": ("list.bullet.clipboard", "Planning an improvement"),
    "revise_plan": ("list.bullet.clipboard", "Revising a plan"),
    "start_task": ("hammer", "Starting a task"),
    "stop_task": ("stop.circle", "Stopping a task"),
    "task_detail": ("list.bullet.rectangle", "Checking on a task"),
    "run_command": ("terminal", "Running a command"),
    "job_status": ("gearshape.2", "Checking a background job"),
    "inspect": ("magnifyingglass", "Looking through my own code"),
    "helper_detail": ("person.2", "Checking on a helper"),
    "attach_image": ("photo", "Sending a picture"),
    "send_text": ("message", "Preparing a text"),
    "make_call": ("phone", "Preparing a call"),
    "set_timer": ("timer", "Setting a timer"),
    "set_alarm": ("alarm", "Setting an alarm"),
    "play_music": ("music.note", "Playing music"),
    "media_control": ("playpause", "Controlling playback"),
    "click": ("cursorarrow.click", "Clicking for you"),
    "mac": ("desktopcomputer", "Working your Mac"),
    "show": ("macwindow", "Putting it on the panel"),
    "panel": ("macwindow", "Adjusting the panel"),
    "deploy_build": ("shippingbox", "Deploying a build"),
    "switch_model": ("cpu", "Switching models"),
}


def set_state(source: str, state: str, until: float | None = None, detail: str = "", **extra) -> None:
    """Record `source`'s state. `until` is when it should expire (busy states default to
    BUSY_TTL from now; 'waiting' and 'idle' don't expire). `extra` (e.g. symbol) is stored as is."""
    now = time.time()
    if until is None and state not in ("waiting", "idle"):
        until = now + BUSY_TTL
    with _lock:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(PATH.parent / ".status.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)  # the listener, the ears and the HUD all write here
            data = read()
            data[source] = {"state": state, "since": now, "until": until, "detail": detail[:80], **extra}
            fd, tmp = tempfile.mkstemp(dir=PATH.parent, prefix=".status-")
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            os.replace(tmp, PATH)  # atomic: the menu bar never reads half a file


def show_action(kind: str, ttl: float = 60.0) -> None:
    """Put `kind` (a key of ACTIONS) on the menu bar and HUD until it's replaced, finished or `ttl` passes."""
    if kind not in ACTIONS:
        return
    symbol, label = ACTIONS[kind]
    try:
        set_state("action", "acting", until=time.time() + ttl, detail=label, symbol=symbol, kind=kind)
    except OSError:
        pass  # cosmetic: never let the indicator break the work


def end_action(started: float | None = None) -> None:
    """The action is done. It lingers until MIN_SHOW after `started`, so even a quick one is seen."""
    try:
        entry, now = read().get("action") or {}, time.time()
        if entry.get("state") != "acting" or (entry.get("until") or 0) <= now:
            return
        set_state("action", "acting", until=max(now, (started or entry.get("since") or now) + MIN_SHOW),
                  **{k: entry[k] for k in ("detail", "symbol", "kind") if k in entry})
    except OSError:
        pass


@contextmanager
def doing(kind: str):
    """`with doing("screenshot"): ...` shows that action while the block runs."""
    started = time.time()
    show_action(kind)
    try:
        yield
    finally:
        if kind in ACTIONS:
            end_action(started)


def read() -> dict:
    try:
        return json.loads(PATH.read_text())
    except (OSError, ValueError):
        return {}


def speech_seconds(text: str) -> float:
    """Rough time `say` takes to speak this, for the 'speaking' indicator."""
    words = max(1, len(text.split()))
    return 0.6 + words * 60 / max(80, config.VOICE_RATE)
