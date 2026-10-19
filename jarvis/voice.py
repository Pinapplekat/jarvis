"""JARVIS speaking out loud on the Mac (macOS `say`, free, on-device).

Speech is only used when Elijah is at the Mac (recent keyboard/mouse activity) and it's
not quiet hours. Telegram always gets the text too, so nothing is lost if he's away.
"""

import os
import queue
import re
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from typing import Callable

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


def speakable(text: str, limit: int = MAX_SPOKEN) -> str:
    """Make text sound right aloud: no emoji or links, one line, not too long."""
    text = URL.sub("the link", EMOJI.sub("", text))
    text = re.sub(r"(?m)^\s*-\s+", "", text)  # list dashes
    text = " ".join(text.split())
    if len(text) > limit:
        cut = text[:limit]
        text = cut[: cut.rfind(". ") + 1] or cut  # end on a sentence when possible
    return text


_speaking: subprocess.Popen | None = None  # the `say` started by speak(), so it can be stopped
ANNOUNCE_PID = config.DATA_DIR / "announcing.pid"  # an announcement's `say`, so the desk mic can stop it
HOLD_MAX_SECONDS = 300   # an announcement waits at most this long for a conversation to end
REPEAT_WINDOW = 600      # the same words aren't said aloud twice within this many seconds


def speak(text: str) -> bool:
    """Say it out loud without blocking. Returns False if there was nothing to say."""
    global _speaking
    spoken = speakable(text)
    if not spoken:
        return False
    _speaking = subprocess.Popen(["say", "-v", config.VOICE_NAME, "-r", str(config.VOICE_RATE), spoken],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        ANNOUNCE_PID.write_text(str(_speaking.pid))
    except (OSError, AttributeError):
        pass
    return True


def stop_announcement() -> bool:
    """Stop an announcement another JARVIS process is saying (he just woke JARVIS at the desk, so only
    one voice talks). Only ever stops a `say` process. True if one was stopped."""
    import signal
    try:
        pid = int(ANNOUNCE_PID.read_text().strip())
    except (OSError, ValueError):
        return False
    try:
        name = subprocess.run(["ps", "-p", str(pid), "-o", "comm="], capture_output=True, text=True,
                              timeout=3).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return False
    if not name.endswith("say"):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    return True


def stop_speaking() -> bool:
    """Cut off whatever speak() is saying. True if something was still talking."""
    global _speaking
    proc, _speaking = _speaking, None
    if proc is None or proc.poll() is not None:
        return False
    try:
        proc.terminate()
    except OSError:
        return False
    return True


def synthesize(text: str) -> str:
    """Render one sentence to an AIFF file in JARVIS's voice; returns its path."""
    fd, path = tempfile.mkstemp(prefix="jarvis-say-", suffix=".aiff")
    os.close(fd)
    subprocess.run(["say", "-v", config.VOICE_NAME, "-r", str(config.VOICE_RATE), "-o", path, text],
                   check=True, timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return path


def play_file(path: str) -> subprocess.Popen:
    return subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class SpeakQueue:
    """Speak sentences in order as they arrive, rendering the next one while the current one
    plays, so a streaming reply starts talking after its first sentence.

    `synth(text) -> clip` renders, `play(clip)` starts playback and returns something with
    .wait() and .terminate(), `discard(clip)` cleans up. `on_play(text)` fires as each
    sentence starts; `first_audio_at` is when the first one since `reset()` began."""

    def __init__(self, synth: Callable[[str], object] = synthesize, play: Callable[[object], object] = play_file,
                 discard: Callable[[object], None] | None = None, on_play: Callable[[str], None] | None = None):
        self._synth, self._play = synth, play
        self._discard = discard or (lambda clip: _unlink(clip) if synth is synthesize else None)
        self.on_play = on_play or (lambda text: None)
        self._texts: queue.Queue = queue.Queue()
        self._clips: queue.Queue = queue.Queue()
        self._gen, self._pending, self._playing = 0, 0, None
        self._cond = threading.Condition()
        self.first_audio_at: float | None = None
        for target in (self._render_loop, self._play_loop):
            threading.Thread(target=target, daemon=True).start()

    def say(self, text: str) -> None:
        spoken = speakable(text)
        if not spoken:
            return
        with self._cond:
            self._pending += 1
            self._texts.put((self._gen, spoken))

    @property
    def playing(self) -> bool:
        """Is a sentence coming out of the speakers right now?"""
        return self._playing is not None

    def reset(self) -> None:
        """Start timing a new reply."""
        self.first_audio_at = None

    def cancel(self) -> None:
        """Drop everything queued and stop what's playing (a newer reply takes over)."""
        with self._cond:
            self._gen += 1
            for q in (self._texts, self._clips):
                while True:
                    try:
                        item = q.get_nowait()
                    except queue.Empty:
                        break
                    self._pending -= 1
                    if q is self._clips and item[1] is not None:
                        self._discard(item[1])
            playing = self._playing
            self._cond.notify_all()
        if playing is not None:
            try:
                playing.terminate()
            except OSError:
                pass

    def wait(self, timeout: float = 300) -> bool:
        """Block until everything queued has been spoken (or dropped). False on timeout."""
        deadline = time.time() + timeout
        with self._cond:
            while self._pending > 0:
                left = deadline - time.time()
                if left <= 0:
                    return False
                self._cond.wait(left)
        return True

    def _done_one(self) -> None:
        with self._cond:
            self._pending -= 1
            self._cond.notify_all()

    def _render_loop(self) -> None:
        while True:
            gen, text = self._texts.get()
            try:
                clip = self._synth(text) if gen == self._gen else None
            except Exception:
                clip = None  # one bad sentence shouldn't silence the rest
            with self._cond:
                if gen != self._gen:  # cancelled while rendering
                    if clip is not None:
                        self._discard(clip)
                    self._pending -= 1
                    self._cond.notify_all()
                    continue
                self._clips.put((gen, clip, text))

    def _play_loop(self) -> None:
        while True:
            gen, clip, text = self._clips.get()
            try:
                with self._cond:
                    if clip is None or gen != self._gen:
                        continue
                    self._playing = proc = self._play(clip)
                if self.first_audio_at is None:
                    self.first_audio_at = time.time()
                self.on_play(text)
                proc.wait()
            except Exception:
                pass
            finally:
                with self._cond:
                    self._playing = None
                if clip is not None:
                    self._discard(clip)
                self._done_one()


def _unlink(path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def should_speak(now: datetime, idle: float | None = None) -> bool:
    return (config.VOICE and not in_quiet_hours(now.time(), parse_quiet_hours(config.QUIET_HOURS))
            and user_present(idle))


def _desk_busy() -> bool:
    from jarvis.speech import busy
    return busy()


class Announcer:
    """A messenger that also speaks when Elijah is at the Mac. Wraps any `send(text)` messenger.

    One voice: while JARVIS is answering him at the desk (or the conversation is still open for a
    follow-up), the spoken copy waits in the background until it's over, so two voices never
    talk at once; the same words aren't spoken twice in a row; quiet hours and presence are checked
    again when it's finally said."""

    def __init__(self, messenger, mem=None, now=None, speaker=speak, presence=should_speak, busy=_desk_busy,
                 defer=None, sleep=time.sleep):
        self.messenger, self.mem = messenger, mem
        self._now, self._speak, self._presence, self._busy = now or datetime.now, speaker, presence, busy
        self._defer = defer or (lambda fn: threading.Thread(target=fn, daemon=True).start())
        self._sleep = sleep
        self._said: dict[str, float] = {}

    def send(self, text, buttons=None, spoken: str | None = None):
        """Text always goes to the messenger (if any); `spoken` (default: the text) is said
        aloud when Elijah is at the Mac."""
        result = None
        if self.messenger:
            result = self.messenger.send(text, buttons=buttons) if buttons else self.messenger.send(text)
        say = spoken or text
        if not self._presence(self._now()):
            return result
        if self._busy():
            self._defer(lambda: self._say_when_free(say))
        else:
            self._say(say)
        return result

    def _say_when_free(self, say: str) -> None:
        waited = 0.0
        while self._busy() and waited < HOLD_MAX_SECONDS:
            self._sleep(1.0)
            waited += 1.0
        if self._busy() or not self._presence(self._now()):
            if self.mem:
                self.mem.log("spoke", f"(held, then skipped: still talking with him or he left) {speakable(say)[:150]}")
            return
        self._say(say)

    def _say(self, say: str) -> None:
        key, now = speakable(say), time.time()
        self._said = {k: t for k, t in self._said.items() if now - t < REPEAT_WINDOW}
        if key in self._said:
            return
        if self._speak(say):
            self._said[key] = now
            _report_speaking(key)
            if self.mem:
                self.mem.log("spoke", key[:200])


def _report_speaking(text: str) -> None:
    import time

    from jarvis.status import set_state, speech_seconds
    try:
        set_state("voice", "speaking", until=time.time() + speech_seconds(text))
    except OSError:
        pass  # the indicator is cosmetic
