"""The desk: Elijah talks to the Mac's microphone and Falak answers aloud. The node hears and plays (wake word, speech
detection, echo cancellation, barge-in); this side turns his utterances into text, runs the turn, and speaks the reply
a sentence at a time while the rest is still being written."""

from __future__ import annotations

import asyncio
import itertools
import logging
import re
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

from jarvis.core.channel import ChannelUnavailable, Deliver, Incoming
from jarvis.channels.desk_speech import DeskReply, DeskSpeech, TurnTimes
from jarvis.protocol import messages as m
from jarvis.speech import Listener, VoiceManager, VoiceProfile

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
# A turn that trails off ("and...", "um") is held for a moment: he usually carries on, and the two parts are one request.
TRAILING = re.compile(r"(?:\b(?:and|um+|uh+|er|so|but|or|then|because)\b|…|\.\.\.)[\s,.]*$", re.IGNORECASE)
GRACE_S = 2.5
MAX_GRACES = 2


class DeskChannel:
    name = NAME

    def __init__(self, *, listener: Callable[[], Listener], voice: VoiceManager, engine: Callable[[], str],
                 profile: Callable[[], VoiceProfile], send_frame: Callable[[str, str | bytes], bool],
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._listener_factory, self._listener = listener, None
        self._voice, self._engine, self._profile, self._send_frame, self._clock = voice, engine, profile, send_frame, clock
        self._deliver: Deliver | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = asyncio.Event()
        self._utterances: dict[int, tuple[str, bytearray]] = {}  # stream -> (node, PCM so far)
        self._speeches: dict[int, DeskSpeech] = {}
        self._times: dict[int, TurnTimes] = {}
        self._node = ""  # the desk node heard from most recently
        self._presence: tuple[float, float, bool] | None = None  # (when, idle_s, conversation open)
        self._ids = itertools.count(SPEAK_FIRST_STREAMS)
        self._warmed = False
        self._held: tuple[str, str, int, TurnTimes, int] | None = None  # text, node, stream, clock, graces used
        self._release_timer: asyncio.TimerHandle | None = None

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
            self._warm()
        elif isinstance(msg, m.VoiceEnd) and msg.stream in self._utterances:
            self._times[msg.stream] = TurnTimes(self._clock())
            asyncio.ensure_future(self._heard(msg.stream))
        elif isinstance(msg, m.PlaybackStopped) and (speech := self._speeches.get(msg.stream)):
            speech.playback_stopped(msg.played_ms, msg.reason)
        elif isinstance(msg, m.Presence):
            self._presence = (self._clock(), msg.idle_s, msg.conversation)

    def _warm(self) -> None:
        """A conversation is starting: load the voice now so the first sentence doesn't wait for the model."""
        if not self._warmed and not self._voice.loaded:
            self._warmed = True
            engine, profile = self._engine(), self._profile()
            asyncio.ensure_future(asyncio.to_thread(self._warm_quietly, engine, profile))

    def _warm_quietly(self, engine: str, profile: VoiceProfile) -> None:
        try:
            self._voice.warm(engine, profile)
        except Exception:
            log.exception("could not load the %s voice", engine)
        finally:
            self._warmed = False

    async def _heard(self, stream: int) -> None:
        node, pcm = self._utterances.pop(stream)
        times = self._times.pop(stream)
        try:
            text = await asyncio.to_thread(self._transcribe, bytes(pcm))
        except Exception:
            log.exception("could not transcribe an utterance")
            await self._say_now(node, SPOKEN_ERROR)
            return
        times.transcript = self._clock()
        if not text.strip() or self._deliver is None:
            return  # a cough or a click
        log.info("desk heard (%.1fs of audio): %r", len(pcm) / 32000, text[:80])
        graces = 0
        if self._held is not None:  # he carried on: one request in two parts
            held_text, _, _, held_times, graces = self._held
            self._cancel_hold()
            text, times.speech_ended = f"{held_text} {text.strip()}", held_times.speech_ended
        if TRAILING.search(text) and graces < MAX_GRACES:
            self._held = (text.strip(), node, stream, times, graces + 1)
            self._release_timer = asyncio.get_running_loop().call_later(
                GRACE_S, lambda: asyncio.ensure_future(self._release()))
            return
        await self._deliver(Incoming(NAME, text.strip(), reply_to=DeskReply(node, stream, times)))

    def _cancel_hold(self) -> None:
        self._held = None
        if self._release_timer is not None:
            self._release_timer.cancel()
            self._release_timer = None

    async def _release(self) -> None:
        """He did not carry on: the held request goes ahead as it was."""
        held, self._held, self._release_timer = self._held, None, None
        if held is not None and self._deliver is not None:
            text, node, stream, times, _ = held
            await self._deliver(Incoming(NAME, text, reply_to=DeskReply(node, stream, times)))

    def _transcribe(self, pcm: bytes) -> str:
        if self._listener is None:
            self._listener = self._listener_factory()
        return self._listener.transcribe(pcm, 16000)

    # ---- the channel interface --------------------------------------------------------------------------------

    async def start(self, deliver: Deliver) -> None:
        self._deliver, self._loop = deliver, asyncio.get_running_loop()
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), 60)
            except asyncio.TimeoutError:
                self._voice.tick()  # unload the voice after voice_idle_minutes without use

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
        speech = DeskSpeech(node, stream, self._synthesize, self._profile, lambda f: self._send_frame(node, f), times,
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

    # ---- speaking -------------------------------------------------------------------------------------------

    async def _synthesize(self, sentence: str, profile: VoiceProfile) -> bytes:
        engine = self._engine()
        try:
            return await asyncio.to_thread(self._voice.synthesize, engine, sentence, profile)
        except Exception:
            if engine == "say":
                raise
            log.exception("the %s voice failed; using macOS say for this sentence", engine)
            return await asyncio.to_thread(self._voice.synthesize, "say", sentence, VoiceProfile(voice="Daniel"))


LEGACY_MODELS = Path("~/Library/Application Support/JARVIS/models").expanduser()  # the old JARVIS's; read only


def create(*, data_dir: Path, engine: Callable[[], str], profile: Callable[[], VoiceProfile],
           send_frame: Callable[[str, str | bytes], bool], idle_minutes: float) -> DeskChannel:
    """The desk with the real engines. Models are looked for in Falak's own folder first, then in the old JARVIS's
    (read only); `jarvis voice setup` puts what is missing in Falak's."""
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
        path = find(dirs, KOKORO_REPO if name == "kokoro" else OMNIVOICE_REPO, KOKORO_FOLDER if name == "kokoro" else "omnivoice")
        if path is None:
            raise FileNotFoundError(f"the {name} voice isn't downloaded; run `jarvis voice setup`")
        return KokoroVoice(path) if name == "kokoro" else OmniVoiceVoice(path)

    return DeskChannel(listener=listener, voice=VoiceManager(make_voice, idle_minutes), engine=engine, profile=profile,
                       send_frame=send_frame)
