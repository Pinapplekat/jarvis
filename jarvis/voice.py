"""JARVIS speaking out loud on the Mac (macOS `say`, free, on-device).

Speech is only used when Elijah is at the Mac (recent keyboard/mouse activity) and it's
not quiet hours. Telegram always gets the text too, so nothing is lost if he's away.
"""

import re
import subprocess
from datetime import datetime

from jarvis import config
from jarvis.alerts import in_quiet_hours, parse_quiet_hours

MAX_SPOKEN = 400
EMOJI = re.compile("[\U0001F000-\U0001FAFF⌀-⏿☀-➿⬀-⯿️‍]")
URL = re.compile(r"https?://\S+")


def idle_seconds() -> float | None:
    """Seconds since the last keyboard/mouse input, or None if it can't be read."""
    try:
        out = subprocess.run(["ioreg", "-c", "IOHIDSystem", "-d", "4"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
    return int(m[1]) / 1e9 if m else None


def user_present(idle: float | None = None) -> bool:
    idle = idle_seconds() if idle is None else idle
    return idle is not None and idle <= config.VOICE_IDLE_SECONDS


def speakable(text: str) -> str:
    """Make text sound right aloud: no emoji or links, one line, not too long."""
    text = URL.sub("the link", EMOJI.sub("", text))
    text = re.sub(r"(?m)^\s*-\s+", "", text)  # list dashes
    text = " ".join(text.split())
    if len(text) > MAX_SPOKEN:
        cut = text[:MAX_SPOKEN]
        text = cut[: cut.rfind(". ") + 1] or cut  # end on a sentence when possible
    return text


def speak(text: str) -> bool:
    """Say it out loud without blocking. Returns False if there was nothing to say."""
    spoken = speakable(text)
    if not spoken:
        return False
    subprocess.Popen(["say", "-v", config.VOICE_NAME, "-r", str(config.VOICE_RATE), spoken],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return True


def should_speak(now: datetime, idle: float | None = None) -> bool:
    return (config.VOICE and not in_quiet_hours(now.time(), parse_quiet_hours(config.QUIET_HOURS))
            and user_present(idle))


class Announcer:
    """A messenger that also speaks when Elijah is at the Mac. Wraps any `send(text)` messenger."""

    def __init__(self, messenger, mem=None, now=None, speaker=speak, presence=should_speak):
        self.messenger, self.mem = messenger, mem
        self._now, self._speak, self._presence = now or datetime.now, speaker, presence

    def send(self, text, buttons=None, spoken: str | None = None):
        """Text always goes to the messenger (if any); `spoken` (default: the text) is said
        aloud when Elijah is at the Mac."""
        result = None
        if self.messenger:
            result = self.messenger.send(text, buttons=buttons) if buttons else self.messenger.send(text)
        say = spoken or text
        if self._presence(self._now()) and self._speak(say) and self.mem:
            self.mem.log("spoke", speakable(say)[:200])
        return result
