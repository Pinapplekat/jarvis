"""'Hey Jarvis' at the desk: an on-device wake word, then speech-to-text, an answer, and
JARVIS speaking it. Audio is processed in memory; nothing is recorded to disk, and nothing
is transcribed unless the wake word was heard (or it's a follow-up right after an answer).

Runs as the `jarvis ears` background job under the "JARVIS Ears" app, which holds the
Microphone permission.
"""

import subprocess
import time
from collections import deque
from typing import Callable

import numpy as np

from jarvis import config

RATE = 16000
CHUNK = 1280                 # 80 ms, what openWakeWord expects
CHIME = "/System/Library/Sounds/Tink.aiff"
MISS = "/System/Library/Sounds/Pop.aiff"
MAX_UTTERANCE = 15.0         # seconds
END_SILENCE = 1.0            # seconds of quiet that end an utterance
START_TIMEOUT = 4.0          # seconds to start talking after the chime
HEARTBEAT_TTL = 30.0         # indicator shows "off" if the listener hasn't checked in this long


def rms(chunk: np.ndarray) -> float:
    x = chunk.astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(x * x))) if x.size else 0.0


class Endpointer:
    """Decides when an utterance has started and ended, from chunk loudness relative to the
    room's noise floor (measured while waiting for the wake word)."""

    def __init__(self, noise_floor: float, start_timeout: float = START_TIMEOUT):
        self.threshold = max(noise_floor * 3.0, 0.008)
        self.start_timeout = start_timeout
        self.started = False
        self.elapsed = self.quiet = 0.0

    def feed(self, level: float, seconds: float) -> str:
        """Returns 'waiting', 'speaking', 'done' or 'nothing' (never started)."""
        self.elapsed += seconds
        if level >= self.threshold:
            self.started, self.quiet = True, 0.0
        elif self.started:
            self.quiet += seconds
        if not self.started:
            return "nothing" if self.elapsed >= self.start_timeout else "waiting"
        if self.quiet >= END_SILENCE or self.elapsed >= MAX_UTTERANCE:
            return "done"
        return "speaking"


class Ears:
    def __init__(self, answer: Callable[..., str], transcribe: Callable[[np.ndarray], str],
                 log=print, speak: Callable[[str], None] | None = None, play: Callable[[str], None] | None = None,
                 speak_async: Callable[[str], object] | None = None, report: Callable | None = None):
        """`answer(text, on_first)`; `speak` blocks until done; `speak_async` starts speaking
        and returns something with .wait() (used to say the first sentence early). `report`
        publishes state for the menu bar indicator."""
        self.answer, self.transcribe, self.log = answer, transcribe, log
        self.speak = speak or _say_and_wait
        self.speak_async = speak_async or _say_async
        self.report = report or _report
        self.play = play or (lambda path: subprocess.Popen(["afplay", path]))
        self.noise = deque(maxlen=50)  # recent chunk levels while idle (~4 s)
        self._wake = None

    # ----- audio plumbing (real mic) -----
    def _wake_model(self):
        if self._wake is None:
            from openwakeword.model import Model
            from openwakeword.utils import download_models
            # Model files live inside the installed package, so a fresh deploy has none yet.
            download_models(model_names=["hey_jarvis"])  # no-op when already present
            self._wake = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
        return self._wake

    def run_forever(self) -> None:
        import sounddevice as sd
        model = self._wake_model()
        self.log(f"listening for 'hey jarvis' (threshold {config.WAKE_THRESHOLD})")
        with sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=CHUNK) as stream:
            checked = 0
            while True:
                if checked % 125 == 0:  # heartbeat every ~10 s; a stopped listener shows as off
                    self.report("waiting", until=time.time() + HEARTBEAT_TTL)
                chunk = stream.read(CHUNK)[0][:, 0]
                self.noise.append(rms(chunk))
                checked += 1
                if checked == 125:  # ~10 s in: macOS feeds pure silence when mic access is denied
                    level = float(np.median(self.noise))
                    self.log(f"mic check: room level {level:.5f}" + (
                        " (exact silence: allow 'JARVIS Ears' in System Settings → Privacy & Security"
                        " → Microphone, then restart)" if level == 0 else " (hearing the room, good)"))
                score = max(model.predict(chunk).values())
                if score >= config.WAKE_THRESHOLD:
                    self.log(f"wake ({score:.2f})")
                    self.converse(stream)
                    model.reset()
                    _drain(stream)
                    self.report("waiting", until=time.time() + HEARTBEAT_TTL)

    # ----- one conversation -----
    def converse(self, stream) -> None:
        """Chime, then answer utterances until a follow-up window passes in silence."""
        from jarvis.status import speech_seconds
        self.play(CHIME)
        self.report("listening")
        timeout = START_TIMEOUT
        while True:
            audio = self.record(lambda: stream.read(CHUNK)[0][:, 0], timeout)
            if audio is None:
                if timeout == START_TIMEOUT:
                    self.play(MISS)  # woke but heard nothing
                return
            self.report("working", detail="thinking")
            text = self.transcribe(audio.astype(np.float32) / 32768.0)
            if not text:
                self.play(MISS)
                return
            self.log(f"heard: {text[:120]}")
            early = {}

            def on_first(sentence):  # start talking while the rest is still being written
                early["sentence"], early["proc"] = sentence, self.speak_async(sentence)
                self.report("speaking", until=time.time() + speech_seconds(sentence))

            reply = self.answer(text, on_first)
            self.log(f"said: {reply[:120]}")
            from jarvis.chat import remainder
            rest = reply
            if early:
                early["proc"].wait()
                left = remainder(reply, early["sentence"])
                rest = reply if left is None else left
            if rest:
                self.report("speaking", until=time.time() + speech_seconds(rest))
                self.speak(rest)           # blocking, so it doesn't hear itself
            _drain(stream)
            self.report("listening", detail="follow-up")
            timeout = config.FOLLOW_UP_SECONDS

    def record(self, read_chunk: Callable[[], np.ndarray], start_timeout: float) -> np.ndarray | None:
        floor = float(np.median(self.noise)) if self.noise else 0.003
        ep = Endpointer(floor, start_timeout)
        chunks = []
        while True:
            chunk = read_chunk()
            chunks.append(chunk)
            state = ep.feed(rms(chunk), CHUNK / RATE)
            if state == "nothing":
                return None
            if state == "done":
                return np.concatenate(chunks)


def _drain(stream) -> None:
    """Throw away audio that piled up while we were busy (including our own voice)."""
    try:
        available = stream.read_available
        if available:
            stream.read(available)
    except Exception:
        pass


def _report(state: str, until: float | None = None, detail: str = "") -> None:
    from jarvis.status import set_state
    try:
        set_state("ears", state, until=until, detail=detail)
    except OSError:
        pass  # the indicator is cosmetic; never let it break listening


def _say_async(text: str) -> subprocess.Popen:
    from jarvis.voice import speakable
    return subprocess.Popen(["say", "-v", config.VOICE_NAME, "-r", str(config.VOICE_RATE), speakable(text)])


def _say_and_wait(text: str) -> None:
    from jarvis.voice import speakable
    subprocess.run(["say", "-v", config.VOICE_NAME, "-r", str(config.VOICE_RATE), speakable(text)],
                   timeout=120)
