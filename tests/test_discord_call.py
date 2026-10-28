"""The Discord call channel against a fake add-on on a real unix socket: no Node, no Discord, no models."""

import asyncio
import json
import shutil
import struct
import subprocess
from pathlib import Path

import pytest
from test_desk_channel import Listener, PcmVoice, until
from test_voice_turns import StreamingBrain

from jarvis.channels import discord_call
from jarvis.channels.discord_call import AUDIO, JSON, CallChannel, CallTaker
from jarvis.channels.voice_services import VoiceServices
from jarvis.core import conversation
from jarvis.core.channel import ChannelUnavailable
from jarvis.host import Host
from jarvis.speech import VoiceManager, VoiceProfile

LOUD, QUIET = b"\x01\x00" * 480, b"\x00\x00" * 480  # 30 ms frames; the fake detector looks at the first byte
HEAD = struct.Struct(">BI")


class FakeChild:
    def __init__(self):
        self.terminated = False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True


class Addon:
    """The Node add-on, played by the test: connects to the socket, sends events and Elijah's audio, records commands."""

    def __init__(self, path):
        self.path, self.reader, self.writer = path, None, None
        self.audio, self.commands = bytearray(), []
        self._pump = None

    async def connect(self):
        self.reader, self.writer = await asyncio.open_unix_connection(self.path)
        self._pump = asyncio.ensure_future(self._read())

    async def _read(self):
        try:
            while True:
                kind, length = HEAD.unpack(await self.reader.readexactly(HEAD.size))
                payload = await self.reader.readexactly(length) if length else b""
                if kind == AUDIO:
                    self.audio += payload
                else:
                    self.commands.append(json.loads(payload))
        except (asyncio.IncompleteReadError, ConnectionError):
            pass

    def event(self, **ev):
        data = json.dumps(ev).encode() + b"\n"
        self.writer.write(HEAD.pack(JSON, len(data)) + data)

    def hear(self, frames):
        for f in frames:
            self.writer.write(HEAD.pack(AUDIO, len(f)) + f)

    async def says(self, loud_frames=12, quiet_frames=26):
        self.hear([LOUD] * loud_frames + [QUIET] * quiet_frames)
        await asyncio.sleep(0)


def make_call(tmp_path, listener, spawned=None, check=lambda: None, channel_id=555):
    services = VoiceServices(lambda: listener, VoiceManager(lambda e: PcmVoice(), 30), lambda: "kokoro", VoiceProfile)
    spawned = spawned if spawned is not None else []

    def spawn(token, owner, socket):
        spawned.append((token, owner, socket))
        return FakeChild()

    channel = CallChannel(services=services, vad=lambda pcm: 0.9 if pcm[0] else 0.0, token="secret-token", owner_id=42,
                          guild_id=7, voice_channel_id=channel_id, run_dir=tmp_path / "run", spawn=spawn, check=check)
    return channel, spawned


def run_call(echo, tmp_path, brain, listener, scenario, **kw):
    call, spawned = make_call(tmp_path, listener, **kw)
    host = Host(echo, brain, [call])

    async def go():
        task = asyncio.create_task(host.run())
        await until(lambda: spawned, "the add-on to be started")
        addon = Addon(spawned[0][2])
        await addon.connect()
        await until(lambda: call._writer is not None, "the add-on's connection")
        try:
            await scenario(host, call, addon)
        finally:
            host.request_stop()
            await task

    asyncio.run(go())
    return spawned


def test_the_add_on_is_started_with_the_token_in_its_environment_not_on_a_command_line(echo, tmp_path):
    seen = {}

    async def scenario(host, call, addon):
        seen["spawn"] = (call._token, str(call._owner))

    spawned = run_call(echo, tmp_path, StreamingBrain(), Listener(), scenario)
    token, owner, socket = spawned[0]
    assert (token, owner) == ("secret-token", "42") and socket.endswith("voice.sock")
    source = inspect_spawn()
    assert "DISCORD_BOT_TOKEN" in source and 'args' not in source.replace("**os.environ", "")  # env, never argv


def inspect_spawn():
    import inspect

    return inspect.getsource(discord_call._spawn_node)


def test_he_joins_the_voice_channel_and_falak_joins_too_and_leaves_when_he_does(echo, tmp_path):
    async def scenario(host, call, addon):
        addon.event(event="owner_voice", guild="7", channel="999")  # another voice channel on the server: ignored
        addon.event(event="owner_voice", guild="8", channel="555")  # another server: ignored
        await asyncio.sleep(0.05)
        assert addon.commands == []
        addon.event(event="owner_voice", guild="7", channel="555")
        await until(lambda: addon.commands == [{"cmd": "join", "guild": "7", "channel": "555"}], "the join command")
        addon.event(event="joined", guild="7", channel="555")
        await until(lambda: call.can_speak_first(), "being in the call")
        addon.event(event="owner_voice", guild="7", channel=None)
        await until(lambda: {"cmd": "leave"} in addon.commands, "the leave command")

    run_call(echo, tmp_path, StreamingBrain(), Listener(), scenario)


def test_a_question_on_the_call_is_answered_a_sentence_at_a_time_with_the_same_pipeline(echo, tmp_path):
    listener = Listener("what's on tomorrow?")

    async def scenario(host, call, addon):
        addon.event(event="joined", guild="7", channel="555")
        await addon.says()
        await until(lambda: {"cmd": "end"} in addon.commands, "the end of the reply")
        # 3 words + 4 words at 400 ms a word, 24 kHz mono s16le
        assert len(addon.audio) == 2 * 24 * 400 * 7
        addon.event(event="idle", ms=2800)
        await until(lambda: call._speech is None, "the reply to finish")
        assert conversation.recent(echo.conn, 3)[-1]["text"] == "One sec, checking. You have two things."

    run_call(echo, tmp_path, StreamingBrain(("One sec, checking. ", "You have two things.")), listener, scenario)
    assert listener.heard and listener.heard[0][1] == 16000


def test_talking_over_falak_on_the_call_stops_the_audio_and_logs_the_spoken_prefix(echo, tmp_path):
    listener = Listener("what's on?", "never mind")
    logged = {}

    async def scenario(host, call, addon):
        addon.event(event="joined", guild="7", channel="555")
        await addon.says()
        await until(lambda: {"cmd": "end"} in addon.commands, "the reply to be sent")
        addon.event(event="played", ms=1200)
        await asyncio.sleep(0.02)
        addon.hear([LOUD] * 10)  # 300 ms of him talking while Falak is still playing
        await until(lambda: {"cmd": "stop"} in addon.commands, "the stop command")
        await until(lambda: "(interrupted)" in conversation.recent(echo.conn, 4)[-1]["text"], "the log to be amended")
        logged["text"] = conversation.recent(echo.conn, 4)[-1]["text"]

    run_call(echo, tmp_path, StreamingBrain(("One sec, checking. ", "You have two things.")), listener, scenario)
    assert logged["text"] == "One sec, checking. (interrupted)"


def test_a_cough_while_falak_speaks_is_not_a_turn(echo, tmp_path):
    taker = CallTaker()
    events = []
    for f in [LOUD] * 5 + [QUIET] * 26:  # 150 ms of noise while busy, then quiet
        events += taker.feed(0.9 if f[0] else 0.0, f, busy=True)
    assert [e[0] for e in events if e[0] != "start"] == []  # no barge-in (under 250 ms) and no utterance


def test_the_taker_cuts_utterances_and_notices_barge_in():
    taker = CallTaker()
    events = []
    frames = [QUIET] * 5 + [LOUD] * 12 + [QUIET] * 24
    for f in frames:
        events += taker.feed(0.9 if f[0] else 0.0, f, busy=False)
    kinds = [e[0] for e in events]
    assert kinds == ["start", "utterance"]
    audio = events[-1][1]
    assert len(audio) >= (12 + 24) * 960 and audio[-960:] == QUIET
    taker = CallTaker()
    events = []
    for f in [LOUD] * 10:
        events += taker.feed(0.9, f, busy=True)
    assert [e[0] for e in events] == ["start", "barge_in"]  # 250 ms over Falak


def test_only_elijahs_audio_is_ever_subscribed_to():
    source = (discord_call.ADDON / "index.js").read_text()
    assert source.count("receiver.subscribe(") == 1 and "receiver.subscribe(OWNER" in source
    assert "speaking.on" not in source  # no event that could pull in someone else's stream


def test_node_20_is_refused_with_the_steps(monkeypatch):
    class Out:
        stdout = "v20.20.2\n"

    assert discord_call.node_version(lambda *a, **k: Out()) == (20, 20)
    with pytest.raises(ChannelUnavailable, match="Node 22.12.*this Mac has 20.20"):
        discord_call.check_node((20, 20))
    with pytest.raises(ChannelUnavailable, match="not installed"):
        discord_call.check_node(None)
    discord_call.check_node((22, 12))
    discord_call.check_node((24, 0))


def test_call_me_sends_a_discord_link_and_only_when_calls_are_set_up(echo, tmp_path):
    from jarvis.core import outbox
    from jarvis.core.actions import Origin
    from jarvis.core.tools import TurnState, build_tools, latest_ask_id

    def tools():
        return {t.name: t for t in build_tools(echo, TurnState(Origin("discord"), latest_ask_id(echo)))}

    assert "falak__call" not in tools()
    call, _ = make_call(tmp_path, Listener())
    echo.call_link = call.link
    out = asyncio.run(tools()["falak__call"].call({}))
    assert not out.is_error
    [row] = outbox.unsent(echo.conn)
    assert row["text"] == "Join me here: https://discord.com/channels/7/555" and row["source"] == "send_text"
    echo.call_link = make_call(tmp_path, Listener(), channel_id=None)[0].link
    assert "falak__call" not in tools()  # no voice channel configured: nothing to link to


def test_a_missing_node_keeps_the_channel_off_with_a_plain_line(echo, tmp_path, caplog):
    def check():
        raise ChannelUnavailable("Discord calls need Node 22.12 or newer (this Mac has 20.20); install it with `brew install node`.")

    call, spawned = make_call(tmp_path, Listener(), check=check)
    host = Host(echo, StreamingBrain(), [call])

    async def go():
        task = asyncio.create_task(host.run())
        await asyncio.sleep(0.1)
        host.request_stop()
        await task

    import logging

    with caplog.at_level(logging.WARNING):
        asyncio.run(go())
    assert spawned == [] and "Node 22.12" in caplog.text


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_the_add_on_is_valid_javascript():
    assert subprocess.run(["node", "--check", str(Path(discord_call.ADDON / "index.js"))], capture_output=True).returncode == 0
