"""One spoken reply at the desk: sentences are synthesized as they complete and streamed to the node, and when Elijah
talks over it the node reports where playback stopped, so the log can say how much was actually heard."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from jarvis.protocol import messages as m
from jarvis.speech import SAMPLE_RATE, SentenceBuffer, VoiceProfile

log = logging.getLogger("jarvis.channels.desk")

FILLER = "One sec."  # said when a lookup starts before Falak has said anything
CHUNK = 1920  # bytes of PCM16 per frame: 40 ms at 24 kHz
PLAYBACK_GRACE = 5.0  # seconds past the audio's length to wait for the node to say it has finished


@dataclass
class TurnTimes:
    """When each stage of one turn happened (monotonic seconds); the log line shows the gaps."""

    speech_ended: float
    transcript: float | None = None
    first_text: float | None = None
    first_audio: float | None = None

    def line(self) -> str:
        def gap(a: float | None, b: float | None) -> str:
            return f"{b - a:.2f}s" if a is not None and b is not None else "-"

        return (f"end of speech -> transcript {gap(self.speech_ended, self.transcript)}, -> first text "
                f"{gap(self.speech_ended, self.first_text)}, -> first audio {gap(self.speech_ended, self.first_audio)}")


@dataclass(frozen=True)
class DeskReply:
    """The `reply_to` of a message from the desk: which node to answer, and the turn's clock."""

    node: str
    stream: int  # the id of his utterance; the reply's audio uses the same id, so the node always knows it
    times: TurnTimes | None = None


def spoken_prefix(sentences: list[str], durations_ms: list[int], played_ms: int) -> str:
    """The text that was heard when playback stopped `played_ms` in: whole sentences, then the share of the next one
    that fits (by words), since a sentence is one block of audio."""
    out, elapsed = [], 0
    for sentence, length in zip(sentences, durations_ms):
        if elapsed + length <= played_ms:
            out.append(sentence)
            elapsed += length
            continue
        words = sentence.split()
        heard = int(len(words) * max(0, played_ms - elapsed) / length) if length else 0
        if heard:
            out.append(" ".join(words[:heard]) + "…")
        break
    return " ".join(out)


def interrupted_note(sentences: list[str], durations_ms: list[int], played_ms: int) -> str:
    prefix = spoken_prefix(sentences, durations_ms, played_ms)
    return f"{prefix} (interrupted)" if prefix else "(interrupted before I said anything)"


@dataclass
class DeskSpeech:
    """The Speech of the desk (see core.channel.Speech)."""

    node: str
    stream: int
    synth: Callable[[str, VoiceProfile], "asyncio.Future[bytes]"]  # sentence, profile -> PCM16 at SAMPLE_RATE
    profile: Callable[[], VoiceProfile]
    send: Callable[[str | bytes], bool]
    times: TurnTimes | None = None
    on_done: Callable[[], object] = lambda: None  # the channel forgets the stream
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    muted: bool = False

    def __post_init__(self) -> None:
        self._buf = SentenceBuffer()
        self._fed = ""
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker = asyncio.ensure_future(self._speak())
        self.sentences: list[str] = []  # what was sent to the node, in order
        self.durations_ms: list[int] = []
        self._seq, self._started = 0, False
        self._filled = False  # "One sec." was said this turn
        self._stopped = asyncio.get_running_loop().create_future()  # (played_ms, reason) from the node
        self._stopped_with: tuple[int, str] | None = None

    # ---- the Speech interface -------------------------------------------------------------------------------

    def on_text(self, delta: str) -> None:
        if self.muted or self.cancelled.is_set():
            return
        if self.times and self.times.first_text is None and delta.strip():
            self.times.first_text = time.monotonic()
        self._fed += delta
        for sentence in self._buf.feed(delta):
            self._queue.put_nowait((sentence, self.profile()))  # the profile is read now, on the loop thread

    def on_tool(self) -> None:
        """A lookup is starting. If nothing has been said yet, say "One sec." so he isn't left in silence while it
        runs; the model's own words follow. Said at most once a turn, and never after it has started talking."""
        if self.muted or self.cancelled.is_set() or self._fed.strip() or self._filled:
            return
        self._filled = True
        self._queue.put_nowait((FILLER, self.profile()))

    def mute(self) -> None:
        self.muted = True
        self.send(m.SpeakStop(self.stream).to_json())

    async def finish(self, reply: str) -> str | None:
        if self.cancelled.is_set() and not reply:
            await self._close()
            return None
        if not self.muted and not self._fed.strip() and reply.strip():
            self.on_text(reply)  # a brain that did not stream
        for sentence in ([] if self.muted else self._buf.flush()):
            self._queue.put_nowait((sentence, self.profile()))
        self._queue.put_nowait(None)
        await self._worker
        if self.muted:
            await self._close()
            return f"{reply} (not spoken: he cut in before I could say it)"
        if not self.cancelled.is_set():  # (after a barge-in the node is already listening to him)
            self.send(m.SpeakEnd(self.stream).to_json())  # also when there was nothing to say: the node moves on
        if self._started:
            await self._playback_finished()
        await self._close()
        stopped = self._stopped_with
        if stopped is not None and stopped[1] in ("barge_in", "stop") and self.sentences:
            return interrupted_note(self.sentences, self.durations_ms, stopped[0])
        return None

    # ---- from the node ----------------------------------------------------------------------------------------

    def playback_stopped(self, played_ms: int, reason: str) -> None:
        self._stopped_with = (played_ms, reason)
        if reason == "barge_in":
            self.cancelled.set()
        if not self._stopped.done():
            self._stopped.set_result(self._stopped_with)

    # ---- inside ---------------------------------------------------------------------------------------------

    async def _speak(self) -> None:
        while (item := await self._queue.get()) is not None:
            sentence, profile = item
            if self.muted or self.cancelled.is_set():
                continue  # drain: nothing more is said
            try:
                pcm = await self.synth(sentence, profile)
            except Exception:
                log.exception("could not speak a sentence")
                continue
            if self.muted or self.cancelled.is_set() or not pcm:
                continue
            if not self._started:
                self._started = True
                self.send(m.SpeakStart(self.stream).to_json())
                if self.times and self.times.first_audio is None:
                    self.times.first_audio = time.monotonic()
                    log.info("desk latency: %s", self.times.line())
            self.sentences.append(sentence)
            self.durations_ms.append(len(pcm) * 1000 // (2 * SAMPLE_RATE))
            for i in range(0, len(pcm), CHUNK):
                self.send(m.AudioFrame(self.stream, self._seq, m.PLAYBACK, pcm[i:i + CHUNK]).to_bytes())
                self._seq += 1

    async def _playback_finished(self) -> None:
        wait = sum(self.durations_ms) / 1000 + PLAYBACK_GRACE
        try:
            await asyncio.wait_for(asyncio.shield(self._stopped), wait)
        except asyncio.TimeoutError:
            log.warning("the desk never said it finished playing stream %d", self.stream)

    async def _close(self) -> None:
        if not self._worker.done():
            self._worker.cancel()
        await asyncio.gather(self._worker, return_exceptions=True)
        self.on_done()
