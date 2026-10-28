"""Discord voice calls. Python can't hear under Discord's end-to-end encryption (DAVE), so a small Node program
(channels/discord_voice/) joins the call; this side starts it, talks to it over a local unix socket, and runs the same
pipeline as the desk: speech detection, the Listener, the turn, sentences, the Voice and barge-in.

Only Elijah's audio is ever transcribed: the add-on subscribes to his voice alone, and nobody else in the channel is
heard. Discord already cancels echo, so interruptions are detected from the speech detector only."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import re
import shutil
import struct
import subprocess
import time
from collections import deque
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

from jarvis.channels.desk_speech import DeskReply, DeskSpeech, TurnTimes
from jarvis.channels.voice_services import Joiner, VoiceServices
from jarvis.core.channel import ChannelUnavailable, Deliver, Incoming
from jarvis.protocol import messages as m
from jarvis.protocol.tracker import FRAME_MS, SpeechTracker

log = logging.getLogger("jarvis.channels.discord_call")

NAME = "discord_call"
SECRETS = ("DISCORD_BOT_TOKEN",)
SETTINGS = {
    "guild_id": {"type": "integer", "minimum": 1},  # Elijah's private server
    "voice_channel_id": {"type": "integer", "minimum": 1},  # the voice channel Falak joins when he does (and the call link)
}
ADDON = Path(__file__).with_name("discord_voice")
AUDIO, JSON = 1, 2
_HEAD = struct.Struct(">BI")
RESTART_AFTER = 30.0  # seconds before a crashed add-on is started again
NEEDS_NODE = "Discord calls need Node 22.12 or newer ({found}); install it with `brew install node`."


def node_version(run=subprocess.run) -> tuple[int, ...] | None:
    try:
        out = run(["node", "--version"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    found = re.match(r"v(\d+)\.(\d+)", out.strip())
    return (int(found[1]), int(found[2])) if found else None


def check_node(version: tuple[int, ...] | None) -> None:
    if version is None or version < (22, 12):
        found = "not installed" if version is None else f"this Mac has {version[0]}.{version[1]}"
        raise ChannelUnavailable(NEEDS_NODE.format(found=found))


class CallTaker:
    """Cuts his side of the call into utterances and spots him talking over Falak. Fed one 30 ms frame at a time with
    the speech detector's probability and whether Falak is speaking or still thinking (`busy`)."""

    def __init__(self, end_silence_ms: int = 700, barge_in_ms: int = 250, start_ms: int = 150, min_ms: int = 240,
                 pre_roll_ms: int = 450, max_s: float = 30.0) -> None:
        self.end_silence_ms, self.barge_in_ms, self.start_ms, self.min_ms = end_silence_ms, barge_in_ms, start_ms, min_ms
        self.max_bytes = int(max_s * 32000)
        self._tracker = SpeechTracker()
        self._pre_roll: deque[bytes] = deque(maxlen=pre_roll_ms // FRAME_MS)
        self._buf: bytearray | None = None
        self._voiced = 0
        self._barged = self._busy_at_start = False

    def feed(self, probability: float, pcm: bytes, busy: bool) -> list[tuple]:
        speech_ms, silence_ms = self._tracker.feed(probability)
        self._pre_roll.append(pcm)
        out: list[tuple] = []
        if self._buf is None:
            if speech_ms < self.start_ms:
                return out
            self._buf = bytearray(b"".join(self._pre_roll))  # includes the frame in hand
            self._voiced, self._barged, self._busy_at_start = 0, False, busy
            out.append(("start",))
        else:
            self._buf += pcm
        self._voiced += FRAME_MS if probability >= self._tracker.threshold else 0
        if busy and not self._barged and speech_ms >= self.barge_in_ms:
            self._barged = True
            out.append(("barge_in",))
        if silence_ms >= self.end_silence_ms or len(self._buf) >= self.max_bytes:
            audio, self._buf = bytes(self._buf), None
            if self._voiced >= self.min_ms and (self._barged or not self._busy_at_start):  # a cough over Falak is not a turn
                out.append(("utterance", audio))
        return out


class CallChannel:
    name = NAME

    def __init__(self, *, services: VoiceServices, vad: Callable[[bytes], float], token: str, owner_id: int,
                 guild_id: int, voice_channel_id: int | None, run_dir: Path, spawn: Callable | None = None,
                 clock: Callable[[], float] = time.monotonic, check: Callable[[], None] | None = None) -> None:
        self.services, self._vad, self._token, self._owner = services, vad, token, owner_id
        self._guild, self._channel, self._run_dir = guild_id, voice_channel_id, run_dir
        self._spawn, self._clock = spawn or _spawn_node, clock
        self._check = check or (lambda: check_node(node_version()))
        self._taker = CallTaker()
        self._deliver: Deliver | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._stop = asyncio.Event()
        self._connected = asyncio.Event()
        self._child = None
        self._speech: DeskSpeech | None = None
        self._speech_ended = False
        self._busy = 0  # turns delivered whose reply is not over
        self._played_ms = 0
        self._in_call = False
        self._joiner = Joiner(self._deliver_text)
        self._ids = itertools.count(1)
        self._pending: dict[int, DeskSpeech] = {}  # speeches the host has opened and not finished

    # ---- the add-on process and its socket ------------------------------------------------------------------

    async def start(self, deliver: Deliver) -> None:
        self._check()
        self._deliver = deliver
        self._run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self._run_dir / "voice.sock"
        path.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(self._on_connection, path=str(path))
        try:
            while not self._stop.is_set():
                self._connected.clear()
                self._child = self._spawn(self._token, str(self._owner), str(path))
                await asyncio.wait({asyncio.ensure_future(self._stop.wait()), asyncio.ensure_future(self._exited())},
                                   return_when=asyncio.FIRST_COMPLETED)
                if self._stop.is_set():
                    break
                log.warning("the Discord call add-on stopped; starting it again in %d seconds", RESTART_AFTER)
                await self._sleep_or_stop(RESTART_AFTER)
        finally:
            server.close()
            self._kill()
            path.unlink(missing_ok=True)

    async def _sleep_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    async def _exited(self) -> None:
        while self._child is not None and self._child.poll() is None:
            await asyncio.sleep(0.5)

    def _kill(self) -> None:
        child, self._child = self._child, None
        if child is not None and child.poll() is None:
            child.terminate()

    async def stop(self) -> None:
        self._stop.set()
        self._send(JSON, b'{"cmd": "leave"}\n')

    async def _on_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writer = writer
        self._connected.set()
        try:
            while True:
                kind, length = _HEAD.unpack(await reader.readexactly(_HEAD.size))
                payload = await reader.readexactly(length) if length else b""
                if kind == AUDIO:
                    self._mic(payload)
                elif kind == JSON:
                    self._event(json.loads(payload))
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except Exception:
            log.exception("the Discord call add-on sent something unreadable")
        finally:
            self._writer, self._in_call = None, False

    def _send(self, kind: int, payload: bytes) -> bool:
        if self._writer is None or self._writer.is_closing():
            return False
        self._writer.write(_HEAD.pack(kind, len(payload)) + payload)
        return True

    def _command(self, **cmd) -> None:
        self._send(JSON, json.dumps(cmd).encode() + b"\n")

    # ---- events from the add-on -----------------------------------------------------------------------------

    def _event(self, ev: dict) -> None:
        kind = ev.get("event")
        if kind == "owner_voice":
            wants = ev.get("channel") is not None and str(ev.get("guild")) == str(self._guild) and (
                self._channel is None or str(ev["channel"]) == str(self._channel))
            if wants and not self._in_call:
                self._command(cmd="join", guild=str(self._guild), channel=str(ev["channel"]))
            elif not wants and self._in_call and ev.get("channel") is None:
                self._command(cmd="leave")
        elif kind == "joined":
            self._in_call = True
            self.services.warm_in_background()
            log.info("joined the call")
        elif kind == "left":
            self._in_call = False
        elif kind == "played":
            self._played_ms = int(ev.get("ms", 0))
        elif kind == "idle":
            self._played_ms = int(ev.get("ms", self._played_ms))
            if self._speech is not None and self._speech_ended:
                self._speech.playback_stopped(self._played_ms, "done")
        elif kind == "error":
            log.error("the Discord call add-on: %s", ev.get("what"))

    # ---- hearing him -------------------------------------------------------------------------------------------

    def _mic(self, pcm: bytes) -> None:
        for event in self._taker.feed(self._vad(pcm), pcm, busy=self._busy > 0):
            if event[0] == "barge_in":
                self._barge_in()
            elif event[0] == "utterance":
                times = TurnTimes(self._clock())
                asyncio.ensure_future(self._heard(event[1], times))

    def _barge_in(self) -> None:
        """He talked over Falak: stop the audio here, then tell the speech where it stopped."""
        self._command(cmd="stop")
        if self._speech is not None:
            self._speech.playback_stopped(self._played_ms, "barge_in")
        else:  # still thinking: the turn is cancelled through the speech the host opened for it
            for speech in list(self._pending.values()):
                speech.playback_stopped(0, "barge_in")

    async def _heard(self, pcm: bytes, times: TurnTimes) -> None:
        try:
            text = await asyncio.to_thread(self.services.transcribe, pcm)
        except Exception:
            log.exception("could not transcribe him on the call")
            return
        times.transcript = self._clock()
        if text.strip():
            log.info("call heard (%.1fs of audio): %r", len(pcm) / 32000, text[:80])
            await self._joiner.offer(text, DeskReply("call", next(self._ids), times))

    async def _deliver_text(self, text: str, reply_to: object) -> None:
        if self._deliver is not None:
            self._busy += 1
            await self._deliver(Incoming(NAME, text, reply_to=reply_to))

    # ---- speaking ---------------------------------------------------------------------------------------------

    def begin_speech(self, reply_to: object) -> DeskSpeech:
        if isinstance(reply_to, DeskReply):
            stream, times = reply_to.stream, reply_to.times
        else:
            stream, times = next(self._ids) + (1 << 30), None
        speech = DeskSpeech("call", stream, self.services.synthesize, self.services.profile, self._speech_frame, times,
                            on_done=lambda: self._speech_over(stream))
        self._speech, self._speech_ended = speech, False
        self._pending[stream] = speech
        return speech

    def _speech_over(self, stream: int) -> None:
        self._pending.pop(stream, None)
        self._busy = max(0, self._busy - 1)
        if self._speech is not None and self._speech.stream == stream:
            self._speech = None

    def _speech_frame(self, frame: str | bytes) -> bool:
        """The desk speaks protocol frames; the add-on wants raw audio and a few commands."""
        if m.is_audio(frame):
            return self._send(AUDIO, m.AudioFrame.from_bytes(bytes(frame)).pcm)
        msg = m.parse(frame)
        if isinstance(msg, m.SpeakEnd):
            self._speech_ended = True
            self._command(cmd="end")
        elif isinstance(msg, m.SpeakStop):
            self._command(cmd="stop")
        return True

    def can_speak_first(self) -> bool:
        return self._in_call

    async def send(self, text: str, *, reply_to: object = None, ask_id: int | None = None, images: tuple = ()) -> None:
        await self.begin_speech(DeskReply("call", next(self._ids) + (1 << 30))).finish(text)

    @asynccontextmanager
    async def typing(self, reply_to: object):
        yield

    def link(self) -> str | None:
        """What "call me" sends him: opening this joins the voice channel (bots can't ring)."""
        return f"https://discord.com/channels/{self._guild}/{self._channel}" if self._channel else None


def _spawn_node(token: str, owner: str, socket: str):
    """Start the add-on. The token travels in its environment, never on a command line another user could read."""
    env = {**os.environ, "DISCORD_BOT_TOKEN": token, "FALAK_OWNER_ID": owner, "FALAK_VOICE_SOCKET": socket}
    if not (ADDON / "node_modules").is_dir():
        raise ChannelUnavailable(f"run `npm install` in {ADDON} first (Node 22.12 or newer)")
    return subprocess.Popen(["node", str(ADDON / "index.js")], env=env, stdin=subprocess.DEVNULL)
