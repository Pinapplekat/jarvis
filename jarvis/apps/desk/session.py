"""The desk's ears and mouth on the node: microphone frames in, host audio out, one thread, no locks.

Everything that happens arrives as an event on one queue (a microphone frame, something the Swift helper reported, a
frame from the host, a clock tick) and is handled in order by `handle`. The state machine decides; this module does
what it says, so barge-in stops the speakers right here with no round trip to the host."""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from typing import Protocol

from jarvis.apps.desk.machine import FOLLOW_UP, IDLE, SPEAKING, DeskMachine, Effect, Timing
from jarvis.protocol.tracker import FRAME_MS, SpeechTracker
from jarvis.protocol import messages as m

log = logging.getLogger("jarvis.apps.desk")

WAKE_CHUNK = 2560  # bytes: 80 ms of 16 kHz PCM16, what openWakeWord wants
PRESENCE_EVERY = 5.0  # seconds
PRE_ROLL_MS = 500  # his first word is spoken before we are sure he is talking


class Helper(Protocol):
    def play(self, pcm: bytes) -> None: ...  # 24 kHz PCM16 for the speakers

    def command(self, cmd: dict) -> None: ...  # {"cmd": "stop" | "chime" | ...}

    def close(self) -> None: ...


class DeskSession:
    def __init__(self, *, helper: Helper, vad: Callable[[bytes], float], wake: Callable[[bytes], bool] | None,
                 send: Callable[[str | bytes], None], timing: Timing = Timing(), chimes: bool = True,
                 clock: Callable[[], float] = time.monotonic, idle_seconds: Callable[[], float | None] = lambda: None,
                 on_error: Callable[[str], None] = lambda what: None) -> None:
        self.helper, self.vad, self.wake, self.send = helper, vad, wake, send
        self.machine, self.tracker = DeskMachine(timing), SpeechTracker()
        self.chimes, self.clock, self.idle_seconds, self.on_error = chimes, clock, idle_seconds, on_error
        self._pre_roll: deque[bytes] = deque(maxlen=PRE_ROLL_MS // FRAME_MS)
        self._wake_buf = bytearray()
        self._streaming, self._flushed = False, False
        self._seq = 0
        self._played_ms = 0
        self._next_presence = 0.0

    @property
    def state(self) -> str:
        return self.machine.state

    # ---- the one entry point ----------------------------------------------------------------------------------

    def handle(self, kind: str, payload: object = None) -> None:
        now = self.clock()
        if kind == "mic":
            self._mic(now, payload)  # type: ignore[arg-type]
        elif kind == "helper":
            self._helper(now, payload)  # type: ignore[arg-type]
        elif kind == "host":
            self._host(now, payload)  # type: ignore[arg-type]
        elif kind == "tick":
            self._run(self.machine.tick(now))
            if now >= self._next_presence:
                self._next_presence = now + PRESENCE_EVERY
                idle = self.idle_seconds()
                if idle is not None:
                    self.send(m.Presence(idle, self.state != IDLE).to_json())

    # ---- microphone -------------------------------------------------------------------------------------------

    def _mic(self, now: float, pcm: bytes) -> None:
        self._flushed = False
        self._pre_roll.append(pcm)
        speech_ms, silence_ms = self.tracker.feed(self.vad(pcm))
        if self.wake is not None and self.state in (IDLE, FOLLOW_UP) and self._wake_heard(pcm):
            self._pre_roll.clear()  # what he said to wake us is not part of what he asks
            self.tracker.reset()
            speech_ms = silence_ms = 0
            self._run(self.machine.wake(now))
        self._run(self.machine.vad(now, speech_ms, silence_ms))
        if self._streaming and not self._flushed:
            self._send_audio(pcm)

    def _wake_heard(self, pcm: bytes) -> bool:
        self._wake_buf += pcm
        heard = False
        while len(self._wake_buf) >= WAKE_CHUNK:
            chunk, self._wake_buf = bytes(self._wake_buf[:WAKE_CHUNK]), self._wake_buf[WAKE_CHUNK:]
            heard = self.wake(chunk) or heard
        return heard

    def _send_audio(self, pcm: bytes) -> None:
        self.send(m.AudioFrame(self.machine.stream, self._seq, m.MIC, pcm).to_bytes())
        self._seq += 1

    # ---- the helper app and the host --------------------------------------------------------------------------

    def _helper(self, now: float, event: dict) -> None:
        kind = event.get("event")
        if kind == "played":
            self._played_ms = int(event.get("ms", 0))
        elif kind == "idle":
            self._played_ms = int(event.get("ms", self._played_ms))
            self._run(self.machine.playback_idle(now))
        elif kind == "hotkey":
            down = event.get("action") == "down"
            self._run(self.machine.push_down(now) if down else self.machine.push_up(now))
        elif kind == "ready":
            echo = event.get("echo_cancellation", True) is not False
            self.machine.voice_barge_in = echo
            if not echo:
                self.on_error("no echo cancellation")
        elif kind == "notice":
            log.warning("the desk helper: %s", event.get("what"))
        elif kind == "error":
            log.error("the desk helper: %s", event.get("what"))
            self.on_error(str(event.get("what", "unknown")))

    def _host(self, now: float, frame: str | bytes) -> None:
        if m.is_audio(frame):
            try:
                audio = m.AudioFrame.from_bytes(bytes(frame))
            except m.ProtocolError:
                return
            if audio.kind == m.PLAYBACK and self.state == SPEAKING and audio.stream == self.machine.reply_stream:
                self.machine.playback_busy()
                self.helper.play(audio.pcm)
            return
        msg = m.parse(frame)
        if isinstance(msg, m.SpeakStart):
            was = self.state
            self._run(self.machine.host_speak_start(now, msg.stream))
            if self.state == SPEAKING and was != SPEAKING:
                self._played_ms = 0
        elif isinstance(msg, m.SpeakEnd):
            self._run(self.machine.host_speak_end(now, msg.stream))
        elif isinstance(msg, m.SpeakStop):
            self._run(self.machine.host_stop(now, msg.stream))

    # ---- doing what the machine says --------------------------------------------------------------------------

    def _run(self, effects: list[Effect]) -> None:
        for effect in effects:
            what = effect[0]
            if what == "chime":
                if self.chimes:
                    self.helper.command({"cmd": "chime", "name": effect[1]})
            elif what == "voice_start":
                self._seq = 0
                self.send(m.VoiceStart(effect[1], effect[2]).to_json())
            elif what == "stream_mic":
                self._streaming = bool(effect[1])
                if self._streaming:
                    for frame in self._pre_roll:  # the last half second, which holds his first word
                        self._send_audio(frame)
                    self._flushed = True
            elif what == "voice_end":
                self.send(m.VoiceEnd(effect[1]).to_json())
            elif what == "stop_playback":
                self.helper.command({"cmd": "stop"})
            elif what == "playback_stopped":
                self.send(m.PlaybackStopped(effect[1], self._played_ms, effect[2]).to_json())

    def close(self) -> None:
        self.helper.close()
