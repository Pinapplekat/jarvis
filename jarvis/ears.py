"""'Hey Jarvis' at the desk: an on-device wake word, then speech-to-text, an answer, and
JARVIS speaking it. Audio is processed in memory; nothing is recorded to disk, and nothing
is transcribed unless the wake word was heard (or it's a follow-up right after an answer).

Runs as the `jarvis ears` background job under the "JARVIS Ears" app, which holds the
Microphone permission.
"""

import math
import subprocess
import threading
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
START_TIMEOUT = 4.0          # seconds to start talking after the chime
RESUME_GAP = 0.25            # a pause this long that he talks through marks him as mid-thought
ECHO_WINDOW = 12             # recent mic levels (~1 s) while JARVIS talks: how loud its own voice is
ECHO_WARMUP = 0.3            # seconds of JARVIS's voice heard before barge-in can trigger
HEARTBEAT_TTL = 30.0         # indicator shows "off" if the listener hasn't checked in this long
SPEAKING, LISTENING = "speaking", "listening"


def rms(chunk: np.ndarray) -> float:
    x = chunk.astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(x * x))) if x.size else 0.0


class Endpointer:
    """One state machine for a turn, from chunk loudness relative to the room's noise floor
    (measured while waiting for the wake word). Two phases:

    SPEAKING (JARVIS is answering): only sustained speech well above the room and above
    JARVIS's own echo counts, and it barges in: the phase switches to LISTENING with that
    speech already counted. LISTENING: the turn ends after a trailing quiet window, which
    grows once he has paused and carried on (mid-thought). Barging in never shortens it."""

    def __init__(self, noise_floor: float, start_timeout: float = START_TIMEOUT, phase: str = LISTENING):
        self.threshold = max(noise_floor * 3.0, 0.008)
        self.hold = max(noise_floor * 2.0, 0.006)  # quieter trailing words still count as speech
        self.barge = self.threshold * config.BARGE_IN_LEVEL
        self.start_timeout, self.phase = start_timeout, phase
        self.started = self.resumed = False
        self.elapsed = self.quiet = self.loud = self.played = 0.0
        self.echo: deque = deque(maxlen=ECHO_WINDOW)

    def feed(self, level: float, seconds: float, playing: bool = False) -> str:
        """LISTENING: 'waiting', 'speaking', 'done' or 'nothing' (never started).
        SPEAKING: 'talking', or 'barge_in' once he has talked over JARVIS. `playing` says
        JARVIS's voice is coming out of the speakers right now."""
        if self.phase == SPEAKING:
            return self._feed_speaking(level, seconds, playing)
        self.elapsed += seconds
        if level >= (self.hold if self.started else self.threshold):
            if self.started and self.quiet >= RESUME_GAP:
                self.resumed = True
            self.started, self.quiet = True, 0.0
        elif self.started:
            self.quiet += seconds
        if not self.started:
            return "nothing" if self.elapsed >= self.start_timeout else "waiting"
        if self.quiet >= self.window() or self.elapsed >= MAX_UTTERANCE:
            return "done"
        return "speaking"

    def window(self) -> float:
        return config.EAR_MAX_SILENCE_SECONDS if self.resumed else config.EAR_END_SILENCE_SECONDS

    def _feed_speaking(self, level: float, seconds: float, playing: bool) -> str:
        echo = float(np.median(self.echo)) if self.echo else 0.0
        if playing:  # learn how loud JARVIS's own voice is at the mic
            self.echo.append(level)
            self.played += seconds
            if self.played < ECHO_WARMUP:
                self.loud = 0.0
                return "talking"
        if level >= max(self.barge, echo * 2.0):
            self.loud += seconds
        else:
            self.loud = 0.0
        if self.loud * 1000 < config.BARGE_IN_MIN_MS:
            return "talking"
        self.phase, self.started, self.quiet, self.elapsed = LISTENING, True, 0.0, self.loud
        return "barge_in"


class Ears:
    def __init__(self, answer: Callable[..., str], transcribe: Callable[[np.ndarray], str],
                 log=print, play: Callable[[str], None] | None = None, voice=None,
                 report: Callable | None = None, timing: Callable[[int], None] | None = None,
                 after_reply: Callable[[], object] | None = None):
        """`answer(text, on_text)` streams the reply's text so far into `on_text`; `voice` is a
        SpeakQueue, so each sentence is spoken as soon as it's written. `report` publishes
        state for the menu bar indicator; `timing(ms)` gets the time to first audio;
        `after_reply()` runs once each answer is over (it passes on helper reports that waited)."""
        self.answer, self.transcribe, self.log = answer, transcribe, log
        self.report = report or _report
        self.play = play or (lambda path: subprocess.Popen(["afplay", path]))
        self.timing = timing or (lambda ms: None)
        self.after_reply = after_reply or (lambda: None)
        self.voice = voice
        self.noise = deque(maxlen=50)  # recent chunk levels while idle (~4 s)
        self._wake = None
        self.barged: tuple[np.ndarray, float] | None = None

    def _voice(self):
        if self.voice is None:  # started on first use, so tests and imports stay cheap
            from jarvis.status import speech_seconds
            from jarvis.voice import SpeakQueue
            self.voice = SpeakQueue(on_play=lambda t: self.report("speaking", until=time.time() + speech_seconds(t)))
        return self.voice

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
        self.play(CHIME)
        self.report("listening")
        timeout, barged = START_TIMEOUT, None
        read = lambda: stream.read(CHUNK)[0][:, 0]
        while True:
            if barged:  # he talked over the last answer: that's already this turn
                audio, heard_at = barged
            else:
                audio = self.record(read, timeout)
                heard_at = time.time()  # he stopped talking: the clock for time to first audio
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
            self.reply(text, heard_at, read)
            barged, self.barged = self.barged, None
            if not barged:
                _drain(stream)
            self.report("listening", detail="follow-up")
            timeout = config.FOLLOW_UP_SECONDS

    def reply(self, text: str, heard_at: float, read_chunk: Callable[[], np.ndarray] | None = None) -> str:
        """Answer out loud, speaking each sentence as soon as the streaming reply finishes it,
        and wait until it's all been said. With `read_chunk` (and BARGE_IN), the mic stays
        open meanwhile: if he talks over JARVIS it stops at once, and what he says ends up
        in `self.barged` as (audio, heard_at) for the next turn."""
        from jarvis.speech import live_reply_ended, live_reply_started
        live_reply_started()  # helper reports wait until this answer is over
        try:
            return self._reply(text, heard_at, read_chunk)
        finally:
            live_reply_ended()  # however it ended (done, barged in on, or an error)
            try:
                self.after_reply()  # pass on what waited
            except Exception as e:
                self.log(f"after reply: {e}")

    def _reply(self, text: str, heard_at: float, read_chunk) -> str:
        from jarvis.speech import SentenceBuffer, sentences
        voice, buf = self._voice(), SentenceBuffer()
        voice.cancel()  # nothing from an earlier answer talks over this one
        voice.reset()
        self.barged = None
        cut, lock, finished = threading.Event(), threading.Lock(), threading.Event()
        monitor = None
        if read_chunk is not None and config.BARGE_IN:
            monitor = threading.Thread(target=self._barge_in_watch, args=(read_chunk, finished, cut, lock),
                                       daemon=True)
            monitor.start()

        def say(sentence: str) -> None:
            with lock:  # never queue a sentence after he has cut in
                if not cut.is_set():
                    voice.say(sentence)

        def on_text(so_far: str) -> None:
            for sentence in buf.feed_text(so_far):
                say(sentence)

        reply = self.answer(text, on_text)
        self.log(f"said: {reply[:120]}")
        if reply.startswith(buf.text):  # the usual case: finish what streamed
            for sentence in buf.feed(reply[len(buf.text):]) + buf.flush():
                say(sentence)
        elif not cut.is_set():  # streaming failed or was a "checking" round: say the real answer instead
            voice.cancel()
            for sentence in sentences(reply):
                say(sentence)
        voice.wait()
        if monitor:
            finished.set()
            monitor.join()  # after a barge-in, this waits until he's finished his sentence
        if voice.first_audio_at:
            ms = int((voice.first_audio_at - heard_at) * 1000)
            self.log(f"time_to_first_audio={ms}ms")
            self.timing(ms)
        return reply

    def _barge_in_watch(self, read_chunk, finished: threading.Event, cut: threading.Event,
                        lock: threading.Lock) -> None:
        """Runs while JARVIS answers. Stops when the answer is over, unless he barged in;
        then it keeps listening until he's done and hands over every chunk since he started."""
        ep = Endpointer(self._floor(), phase=SPEAKING)
        recent = deque(maxlen=math.ceil(config.BARGE_IN_MIN_MS / 1000 * RATE / CHUNK) + 2)
        chunks: list = []
        try:
            while ep.phase == LISTENING or not finished.is_set():
                chunk = read_chunk()
                state = ep.feed(rms(chunk), CHUNK / RATE, playing=getattr(self._voice(), "playing", False))
                if state == "talking":
                    recent.append(chunk)
                elif state == "barge_in":
                    from jarvis.speech import live_reply_ended
                    from jarvis.voice import stop_speaking
                    with lock:
                        cut.set()
                        live_reply_ended()  # he has the floor; JARVIS isn't talking any more
                        self._voice().cancel()  # stop the sentence playing and drop the rest
                    stop_speaking()
                    self.log("barge-in")
                    self.report("listening")
                    chunks = [*recent, chunk]  # the words that triggered it aren't lost
                else:
                    chunks.append(chunk)
                    if state == "done":
                        self.barged = (np.concatenate(chunks), time.time())
                        return
        except Exception as e:  # a mic hiccup mustn't break the answer
            self.log(f"barge-in watch stopped: {e}")

    def _floor(self) -> float:
        return float(np.median(self.noise)) if self.noise else 0.003

    def record(self, read_chunk: Callable[[], np.ndarray], start_timeout: float) -> np.ndarray | None:
        ep = Endpointer(self._floor(), start_timeout)
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
