"""The desk: Elijah talks to the Mac's microphone and Falak answers aloud. The node hears and plays (wake word, speech
detection, echo cancellation, barge-in); this side turns his utterances into text, runs the turn, and speaks the reply
a sentence at a time while the rest is still being written."""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

from jarvis.core.channel import Deliver, Incoming
from jarvis.channels.desk_speech import DeskReply, DeskSpeech, TurnTimes
from jarvis.channels.voice_services import Joiner, VoiceServices
from jarvis.protocol import messages as m
from jarvis.speech import VoiceManager, VoiceProfile

log = logging.getLogger("jarvis.channels.desk")

NAME = "desk"
SECRETS: tuple[str, ...] = ()
SETTINGS = {
    "voice_model": {"type": "string"},  # the brain's model for spoken turns (haiku answers first; see voice-measurements)
    "voice_max_turns": {"type": "integer", "minimum": 1},
}
DEFAULT_VOICE_MODEL = "haiku"
DEFAULT_VOICE_MAX_TURNS = 6
MAX_UTTERANCE_BYTES = 60 * 32000  # a minute of 16 kHz PCM16; longer is cut off
AT_THE_MAC = 60.0  # seconds since his last key or mouse input that still count as being there
PRESENCE_FRESH = 45.0  # a Presence report older than this means the node is gone or quiet
SPEAK_FIRST_STREAMS = 1 << 31  # ids for things Falak says first; his utterances count up from 1 on the node
SPOKEN_ERROR = "Sorry sir, I couldn't make that out."


class DeskChannel:
    name = NAME

    def __init__(self, *, services: VoiceServices, send_frame: Callable[[str, str | bytes], bool],
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.services, self._send_frame, self._clock = services, send_frame, clock
        self._deliver: Deliver | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = asyncio.Event()
        self._utterances: dict[int, tuple[str, bytearray]] = {}  # stream -> (node, PCM so far)
        self._speeches: dict[int, DeskSpeech] = {}
        self._times: dict[int, TurnTimes] = {}
        self._node = ""  # the desk node heard from most recently
        self._presence: tuple[float, float, bool] | None = None  # (when, idle_s, conversation open)
        self._ids = itertools.count(SPEAK_FIRST_STREAMS)
        self._joiner = Joiner(self._deliver_text)

    # ---- frames from the node (arrive in the hub's thread) -------------------------------------------------

    def on_frame(self, node: str, frame: str | bytes) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._handle, node, frame)

    def _handle(self, node: str, frame: str | bytes) -> None:
        self._node = node
        if m.is_audio(frame):
            try:
                audio = m.AudioFrame.from_bytes(bytes(frame))
            except m.ProtocolError as e:
                log.warning("dropped an audio frame: %s", e)
                return
            if audio.kind == m.MIC and audio.stream in self._utterances:
                buf = self._utterances[audio.stream][1]
                if len(buf) < MAX_UTTERANCE_BYTES:
                    buf += audio.pcm
            return
        msg = m.parse(frame)
        if isinstance(msg, m.VoiceStart):
            self._utterances[msg.stream] = (node, bytearray())
            self.services.warm_in_background()
        elif isinstance(msg, m.VoiceEnd) and msg.stream in self._utterances:
            self._times[msg.stream] = TurnTimes(self._clock())
            asyncio.ensure_future(self._heard(msg.stream))
        elif isinstance(msg, m.PlaybackStopped) and (speech := self._speeches.get(msg.stream)):
            speech.playback_stopped(msg.played_ms, msg.reason)
        elif isinstance(msg, m.Presence):
            self._presence = (self._clock(), msg.idle_s, msg.conversation)

    async def _heard(self, stream: int) -> None:
        node, pcm = self._utterances.pop(stream)
        times = self._times.pop(stream)
        try:
            text = await asyncio.to_thread(self.services.transcribe, bytes(pcm))
        except Exception:
            log.exception("could not transcribe an utterance")
            await self._say_now(node, SPOKEN_ERROR)
            return
        times.transcript = self._clock()
        if not text.strip() or self._deliver is None:
            return  # a cough or a click
        log.info("desk heard (%.1fs of audio): %r", len(pcm) / 32000, text[:80])
        await self._joiner.offer(text, DeskReply(node, stream, times))

    async def _deliver_text(self, text: str, reply_to: object) -> None:
        if self._deliver is not None:
            await self._deliver(Incoming(NAME, text, reply_to=reply_to))

    # ---- the channel interface --------------------------------------------------------------------------------

    async def start(self, deliver: Deliver) -> None:
        self._deliver, self._loop = deliver, asyncio.get_running_loop()
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), 60)
            except asyncio.TimeoutError:
                self.services.voice.tick()  # unload the voice after voice_idle_minutes without use

    async def stop(self) -> None:
        self._stop.set()

    def can_speak_first(self) -> bool:
        """A conversation is open, or he was at the Mac in the last minute (the node reports it every few seconds)."""
        if self._presence is None or not self._node:
            return False
        when, idle_s, conversation = self._presence
        return self._clock() - when <= PRESENCE_FRESH and (conversation or idle_s <= AT_THE_MAC)

    def begin_speech(self, reply_to: object) -> DeskSpeech:
        if isinstance(reply_to, DeskReply):
            node, stream, times = reply_to.node, reply_to.stream, reply_to.times
        else:  # Falak speaks first: a stream of its own
            node, stream, times = self._node, next(self._ids), None
        speech = DeskSpeech(node, stream, self.services.synthesize, self.services.profile, lambda f: self._send_frame(node, f), times,
                            on_done=lambda: self._speeches.pop(stream, None))
        self._speeches[stream] = speech
        return speech

    async def send(self, text: str, *, reply_to: object = None, ask_id: int | None = None, images: tuple = ()) -> None:
        """Something Falak says first (the outbox): spoken whole. The ask's question is in `text`; buttons are Discord's."""
        await self._say_now(self._node, text)

    async def _say_now(self, node: str, text: str) -> None:
        await self.begin_speech(DeskReply(node, next(self._ids))).finish(text)

    @asynccontextmanager
    async def typing(self, reply_to: object):
        yield


LEGACY_MODELS = Path("~/Library/Application Support/JARVIS/models").expanduser()  # the old JARVIS's; read only


def make_services(*, data_dir: Path, engine: Callable[[], str], profile: Callable[[], VoiceProfile],
                  idle_minutes: float) -> VoiceServices:
    """The real engines. Models are looked for in Falak's own folder first, then in the old JARVIS's (read only);
    `jarvis voice setup` puts what is missing in Falak's."""
    from jarvis.speech import WhisperListener
    from jarvis.speech.listener import WHISPER_REPO
    from jarvis.speech.models import find
    from jarvis.speech.voice import KOKORO_FOLDER, KOKORO_REPO, OMNIVOICE_REPO, KokoroVoice, OmniVoiceVoice, SayVoice

    dirs = [data_dir / "models", LEGACY_MODELS]

    def listener() -> WhisperListener:
        path = find(dirs, WHISPER_REPO.format(size="base.en"))
        if path is None:
            raise FileNotFoundError("the listening model is missing; run `jarvis voice setup`")
        return WhisperListener(path)

    def make_voice(name: str):
        if name == "say":
            return SayVoice()
        repo, folder = (KOKORO_REPO, KOKORO_FOLDER) if name == "kokoro" else (OMNIVOICE_REPO, "omnivoice")
        path = find(dirs, repo, folder)
        if path is None:
            raise FileNotFoundError(f"the {name} voice isn't downloaded; run `jarvis voice setup`")
        return KokoroVoice(path) if name == "kokoro" else OmniVoiceVoice(path)

    return VoiceServices(listener, VoiceManager(make_voice, idle_minutes), engine, profile)
