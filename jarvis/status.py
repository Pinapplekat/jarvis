"""What JARVIS is doing right now, for the menu bar indicator.

Each part writes its own entry to one small JSON file:
  ears     - waiting (for "Hey Jarvis") | listening | working | speaking
  telegram - working (answering a message) | idle
  voice    - speaking (reminders, alerts, interjections)
Busy states carry an expiry so a crash can never leave the indicator stuck; the ears entry
is refreshed as a heartbeat so a stopped listener shows as off.
"""

import json
import os
import tempfile
import threading
import time

from jarvis import config

PATH = config.DATA_DIR / "status.json"
BUSY_TTL = 120          # seconds a busy state may last without being refreshed
_lock = threading.Lock()


def set_state(source: str, state: str, until: float | None = None, detail: str = "") -> None:
    """Record `source`'s state. `until` is when it should expire (busy states default to
    BUSY_TTL from now; 'waiting' and 'idle' don't expire)."""
    now = time.time()
    if until is None and state not in ("waiting", "idle"):
        until = now + BUSY_TTL
    with _lock:
        data = read()
        data[source] = {"state": state, "since": now, "until": until, "detail": detail[:80]}
        PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=PATH.parent, prefix=".status-")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, PATH)  # atomic: the menu bar never reads half a file


def read() -> dict:
    try:
        return json.loads(PATH.read_text())
    except (OSError, ValueError):
        return {}


def speech_seconds(text: str) -> float:
    """Rough time `say` takes to speak this, for the 'speaking' indicator."""
    words = max(1, len(text.split()))
    return 0.6 + words * 60 / max(80, config.VOICE_RATE)
