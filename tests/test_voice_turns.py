"""Spoken turns in the core: the voice principle, falak__send_text, streaming, interruptions and asks."""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone

from fake_channel import FakeChannel

from jarvis.core import conversation, loop, outbox
from jarvis.core.actions import Origin
from jarvis.core.brain import BrainResult
from jarvis.core.channel import Incoming
from jarvis.core.context import VOICE_PRINCIPLE, system_prompt
from jarvis.core.outbox import Sender
from jarvis.core.tools import TurnState, build_tools, latest_ask_id
from jarvis.core.brain_sdk import _stream
from jarvis.host import Host


class FakeSpeech:
    """What a voice channel gives the loop: collects the text, and Elijah can cut in."""

    def __init__(self):
        self.cancelled = asyncio.Event()
        self.text, self.muted, self.finished_with, self.said = [], False, None, None

    def on_text(self, delta):
        self.text.append(delta)

    def on_tool(self):
        self.text.append("<tool>")

    def mute(self):
        self.muted = True

    async def finish(self, reply):
        self.finished_with = reply
        return self.said


class StreamingBrain:
    """Writes its text in pieces, optionally running tools in between, and waits for the test where it says so."""

    def __init__(self, pieces=("One sec, checking. ", "You have two things."), tool=None, gate=None, hold=None):
        self.pieces, self.tool, self.gate, self.hold = pieces, tool, gate, hold
        self.seen_by_first_sentence = None
        self.systems = []

    async def run(self, system, prompt, tools, on_text=None):
        self.systems.append(system)
        by_name = {t.name: t for t in tools}
        used = []
        for i, piece in enumerate(self.pieces):
            if on_text:
                on_text(piece)
            await asyncio.sleep(0)
            if i == 0 and self.tool:
                await by_name[self.tool[0]].call(self.tool[1])
                used.append(self.tool[0])
            if self.gate and i == 0:
                self.gate.set()
                await self.hold.wait()
        return BrainResult("".join(self.pieces), used, num_turns=1)


def handle(echo, brain, speech, channel="desk", text="what's on tomorrow?"):
    return asyncio.run(loop.handle_message(echo, brain, text, Origin(channel), speech=speech))


def test_the_voice_principle_reaches_the_prompt_on_spoken_turns_only(echo):
    for channel, spoken in (("desk", True), ("discord_call", True), ("discord", False), ("cli", False)):
        brain = StreamingBrain()
        handle(echo, brain, FakeSpeech() if spoken else None, channel)  # only voice channels give a turn a Speech
        assert (VOICE_PRINCIPLE in brain.systems[0]) is spoken, channel
    assert "falak__send_text" in system_prompt("Falak", spoken=True) and "send_text" not in system_prompt("Falak")


def test_send_text_exists_only_on_spoken_turns_and_queues_a_discord_message(echo):
    def names(channel, spoken=True):
        turn = TurnState(Origin(channel), latest_ask_id(echo), spoken=spoken)
        return {t.name: t for t in build_tools(echo, turn)}

    assert "falak__send_text" in names("desk") and "falak__send_text" in names("discord_call")
    assert "falak__send_text" not in names("discord", spoken=False) and "falak__send_text" not in names("cli", spoken=False)
    nudged = []
    echo.outbox_nudge = lambda: nudged.append(1)
    out = asyncio.run(names("desk")["falak__send_text"].call({"text": "pytest -k m8 failed: 2 errors"}))
    assert not out.is_error and nudged == [1]
    [row] = outbox.unsent(echo.conn)
    assert (row["text"], row["source"]) == ("pytest -k m8 failed: 2 errors", "send_text")
    assert asyncio.run(names("desk")["falak__send_text"].call({"text": "  "})).is_error


def test_send_text_goes_to_discord_even_when_the_desk_could_speak(echo):
    echo.settings = replace(echo.settings, quiet_hours="")
    desk, discord = FakeChannel("desk"), FakeChannel("discord")
    desk.can_speak_first = lambda: True
    host = Host(echo, StreamingBrain(), [desk, discord])
    assert host._notify_channel("send_text", "the full error") is discord
    assert host._notify_channel("app:reminders", "Call Emily") is desk
    assert host._notify_channel("app:reminders", "x" * 500) is discord  # long: for reading
    desk.can_speak_first = lambda: False
    assert host._notify_channel("app:reminders", "Call Emily") is discord


def test_the_desk_never_speaks_first_in_quiet_hours(echo):
    hour = datetime.now(echo.settings.tz()).hour
    echo.settings = replace(echo.settings, quiet_hours=f"{hour}-{(hour + 2) % 24}")  # right now is inside it
    desk, discord = FakeChannel("desk"), FakeChannel("discord")
    desk.can_speak_first = lambda: True
    assert Host(echo, StreamingBrain(), [desk, discord])._notify_channel("app:reminders", "Call Emily") is discord


def test_the_first_sentence_is_available_before_the_brain_finishes(echo):
    gate, hold = asyncio.Event(), asyncio.Event()
    speech, seen = FakeSpeech(), []

    async def go():
        brain = StreamingBrain(gate=gate, hold=hold)
        task = asyncio.create_task(loop.handle_message(echo, brain, "hi", Origin("desk"), speech=speech))
        await gate.wait()
        seen.append("".join(speech.text))  # the brain is still running here
        hold.set()
        return await task

    reply = asyncio.run(go())
    assert seen == ["One sec, checking. "] and reply.text.endswith("two things.")


def test_cut_off_before_anything_ran_cancels_the_turn_and_logs_no_reply(echo):
    gate, hold, speech = asyncio.Event(), asyncio.Event(), FakeSpeech()

    async def go():
        brain = StreamingBrain(gate=gate, hold=hold)  # never released: it is "thinking"
        task = asyncio.create_task(loop.handle_message(echo, brain, "what's on?", Origin("desk"), speech=speech))
        await gate.wait()
        speech.cancelled.set()
        return await task

    reply = asyncio.run(go())
    assert reply.error == "interrupted" and reply.text == "" and reply.message_id == 0
    roles = [m["role"] for m in conversation.recent(echo.conn, 5)]
    assert roles == ["user"]  # what he said is kept; there is no answer to keep


def test_cut_off_after_a_tool_ran_finishes_silently_and_keeps_the_result(echo):
    gate, hold, speech = asyncio.Event(), asyncio.Event(), FakeSpeech()

    async def go():
        brain = StreamingBrain(tool=("echo__note", {"text": "milk"}), gate=gate, hold=hold)
        task = asyncio.create_task(loop.handle_message(echo, brain, "note milk", Origin("desk"), speech=speech))
        await gate.wait()
        speech.cancelled.set()
        await asyncio.sleep(0.01)
        hold.set()
        return await task

    reply = asyncio.run(go())
    assert speech.muted and reply.tools == ("echo__note",) and reply.error is None
    assert echo.apps["echo"].module.NOTES == ["milk"]
    assert conversation.recent(echo.conn, 5)[-1]["text"] == reply.text  # the result stays in the conversation


class SpeakingChannel(FakeChannel):
    def __init__(self, name="desk"):
        super().__init__(name)
        self.speech = FakeSpeech()

    def begin_speech(self, reply_to):
        return self.speech


def run_host_turn(echo, brain, channels, incoming):
    asyncio.run(Host(echo, brain, channels).turn(incoming))


def test_the_log_records_what_was_said_up_to_where_playback_stopped(echo):
    desk = SpeakingChannel()
    desk.speech.said = "One sec, checking. (interrupted)"
    run_host_turn(echo, StreamingBrain(), [desk], Incoming("desk", "what's on?"))
    assert desk.sent == []  # a spoken turn is not sent as text
    assert desk.speech.finished_with == "One sec, checking. You have two things."
    assert conversation.recent(echo.conn, 5)[-1]["text"] == "One sec, checking. (interrupted)"


def test_an_ask_is_spoken_and_also_posted_to_discord_with_its_buttons(echo):
    desk, discord = SpeakingChannel(), FakeChannel("discord")
    brain = StreamingBrain(("Shall I send it?",), tool=("echo__send", {"text": "running late", "to": "Emily"}))
    run_host_turn(echo, brain, [desk, discord], Incoming("desk", "text Emily I'm running late"))
    [(text, _, ask_id)] = discord.sent
    assert ask_id is not None and "Emily" in text
    assert desk.sent == [] and echo.apps["echo"].module.SENT == []  # nothing sent until he says yes
    # "yeah, send it" at the desk is a turn that calls answer_ask, which a desk turn may do (it is Elijah's own channel)
    host = Host(echo, StreamingBrain(("Sent.",), tool=("falak__answer_ask", {"ask_id": ask_id, "yes": True})),
                [SpeakingChannel()])
    asyncio.run(host.turn(Incoming("desk", "yeah, send it")))
    assert echo.apps["echo"].module.SENT == [("running late", "Emily", 1)]


def test_sdk_partial_messages_become_text_with_a_break_between_blocks():
    out = []
    spoke = False
    for event in (
        {"type": "content_block_start", "content_block": {"type": "text"}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "One sec."}},
        {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": "{"}},
        {"type": "content_block_start", "content_block": {"type": "text"}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Done."}},
    ):
        spoke = _stream(event, out.append, spoke)
    assert out == ["One sec.", "\n", "Done."]


def test_the_outbox_picks_a_channel_for_each_message(echo):
    a, b = FakeChannel("desk"), FakeChannel("discord")
    sender = Sender(echo.conn, lambda source, text: a if source == "reminder" else b,
                    lambda: datetime.now(timezone.utc), echo.settings.tz())
    outbox.add(echo.conn, "Call Emily", "reminder")
    outbox.add(echo.conn, "the full error", "send_text")
    assert asyncio.run(sender.flush()) == 2
    assert [s[0] for s in a.sent] == ["Call Emily"] and [s[0] for s in b.sent] == ["the full error"]


def test_a_spoken_turn_hears_when_an_app_action_starts(echo):
    speech = FakeSpeech()
    brain = StreamingBrain(pieces=("", "Done."), tool=("echo__note", {"text": "x"}))
    handle(echo, brain, speech)
    assert speech.text[:2] == ["", "<tool>"]  # before the model said anything, the speech knew a lookup began
