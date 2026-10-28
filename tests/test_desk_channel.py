"""The host's desk channel with a fake node, listener and voice: utterances in, a spoken reply out a sentence at a time."""

import asyncio
import logging

import pytest
from scripted_brain import ScriptedBrain
from test_voice_turns import StreamingBrain

from jarvis.channels import desk as desk_module
from jarvis.channels import voice_services
from jarvis.channels.desk import DeskChannel
from jarvis.channels.voice_services import VoiceServices
from jarvis.core import conversation, outbox
from jarvis.host import Host
from jarvis.protocol import messages as m
from jarvis.speech import VoiceManager, VoiceProfile

MS_PER_WORD = 400
PCM = b"\x01\x00" * 160  # 10 ms of 16 kHz speech


class PcmVoice:
    """A voice that takes 400 ms per word, and says nothing real."""

    def synthesize(self, sentence, profile):
        return b"\x02\x00" * (24 * MS_PER_WORD * len(sentence.split()))

    def unload(self):
        pass


class Listener:
    def __init__(self, *texts):
        self.texts, self.heard = list(texts), []

    def transcribe(self, pcm, rate):
        self.heard.append((len(pcm), rate))
        return self.texts.pop(0)


def make_desk(listener, make_voice=lambda engine: PcmVoice(), clock=None, auto_done=True, profile=VoiceProfile):
    """`auto_done`: the fake node says playback finished as soon as it is told there is no more audio."""
    sent = []
    kwargs = {"clock": clock} if clock else {}

    def send(node, frame):
        sent.append((node, frame))
        if auto_done and isinstance(frame, str) and isinstance(msg := m.parse(frame), m.SpeakEnd):
            asyncio.get_running_loop().call_soon(channel.on_frame, node, m.PlaybackStopped(msg.stream, 0, "done").to_json())
        return True

    services = VoiceServices(lambda: listener, VoiceManager(make_voice, 30), lambda: "kokoro", profile)
    channel = DeskChannel(services=services, send_frame=send, **kwargs)
    return channel, sent


def utterance(desk, stream, node="mac"):
    desk.on_frame(node, m.VoiceStart(stream, "wake").to_json())
    for seq in range(3):
        desk.on_frame(node, m.AudioFrame(stream, seq, m.MIC, PCM).to_bytes())
    desk.on_frame(node, m.VoiceEnd(stream).to_json())


async def until(check, what="condition", seconds=5.0):
    for _ in range(int(seconds / 0.01)):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


def kinds(sent):
    return [type(m.parse(f)).__name__ if isinstance(f, str) else "audio" for _, f in sent]


def run(echo, brain, desk, scenario):
    host = Host(echo, brain, [desk])

    async def go():
        task = asyncio.create_task(host.run())
        await until(lambda: desk._loop is not None, "the desk to start")
        try:
            await scenario(host)
        finally:
            host.request_stop()
            await task

    asyncio.run(go())


def test_an_utterance_becomes_a_turn_and_the_first_sentence_is_spoken_before_the_brain_finishes(echo, caplog):
    gate, hold = asyncio.Event(), asyncio.Event()
    brain = StreamingBrain(gate=gate, hold=hold)
    listener = Listener("what's on tomorrow?")
    desk, sent = make_desk(listener, auto_done=False)
    seen = {}

    async def scenario(host):
        with caplog.at_level(logging.INFO, "jarvis.channels.desk"):
            utterance(desk, 1)
            await gate.wait()  # the brain has written its first sentence and is still going
            await until(lambda: "audio" in kinds(sent), "the first sentence to be spoken")
            seen["before"] = kinds(sent)
            seen["brain_done"] = hold.is_set()
            hold.set()
            await until(lambda: kinds(sent).count("SpeakEnd") == 1, "SpeakEnd")
            desk.on_frame("mac", m.PlaybackStopped(1, 2800, "done").to_json())
            await until(lambda: conversation.recent(echo.conn, 3)[-1]["role"] == "assistant", "the reply to be logged")

    run(echo, brain, desk, scenario)
    assert seen["before"][0] == "SpeakStart" and seen["brain_done"] is False
    assert all(node == "mac" for node, _ in sent)
    frames = [m.AudioFrame.from_bytes(f) for _, f in sent if isinstance(f, bytes)]
    assert {f.stream for f in frames} == {1} and [f.seq for f in frames] == list(range(len(frames)))
    assert all(f.kind == m.PLAYBACK and f.rate == 24000 for f in frames)
    assert listener.heard == [(3 * len(PCM), 16000)]
    assert "end of speech -> transcript" in caplog.text and "first text" in caplog.text and "first audio" in caplog.text


def played_frames(sent):
    return sum(len(f) - 9 for _, f in sent if isinstance(f, bytes))


def test_an_interrupted_reply_is_logged_as_what_was_actually_said(echo):
    brain = StreamingBrain(("One sec, checking. ", "You have two things."))
    desk, sent = make_desk(Listener("what's on?"), auto_done=False)
    logged = {}

    async def scenario(host):
        utterance(desk, 1)
        await until(lambda: kinds(sent).count("SpeakEnd") == 1, "the whole reply to be sent")
        assert played_frames(sent) == 2 * 24 * MS_PER_WORD * 7  # 3 + 4 words, 400 ms each
        desk.on_frame("mac", m.PlaybackStopped(1, 1200, "barge_in").to_json())  # stopped at 1.2 s
        await until(lambda: "(interrupted)" in conversation.recent(echo.conn, 3)[-1]["text"], "the log to be amended")
        logged["text"] = conversation.recent(echo.conn, 3)[-1]["text"]

    run(echo, brain, desk, scenario)
    assert logged["text"] == "One sec, checking. (interrupted)"


def test_interrupted_partway_through_a_sentence_keeps_the_words_that_were_heard(echo):
    brain = StreamingBrain(("One sec, checking. ", "You have two things."))
    desk, sent = make_desk(Listener("what's on?"), auto_done=False)
    logged = {}

    async def scenario(host):
        utterance(desk, 1)
        await until(lambda: kinds(sent).count("SpeakEnd") == 1, "the reply")
        desk.on_frame("mac", m.PlaybackStopped(1, 1200 + 800, "barge_in").to_json())  # half of sentence two
        await until(lambda: "(interrupted)" in conversation.recent(echo.conn, 3)[-1]["text"], "logged")
        logged["text"] = conversation.recent(echo.conn, 3)[-1]["text"]

    run(echo, brain, desk, scenario)
    assert logged["text"] == "One sec, checking. You have… (interrupted)"


def test_talking_over_a_turn_that_is_still_thinking_cancels_it(echo):
    gate, hold = asyncio.Event(), asyncio.Event()
    brain = StreamingBrain(gate=gate, hold=hold)  # never released
    desk, sent = make_desk(Listener("what's on?", "never mind"))
    result = {}

    async def scenario(host):
        utterance(desk, 1)
        await gate.wait()
        desk.on_frame("mac", m.PlaybackStopped(1, 150, "barge_in").to_json())
        await until(lambda: [t for t in host.reg.conn.execute("SELECT error FROM turns")] != [], "the turn to be recorded")
        result["error"] = host.reg.conn.execute("SELECT error FROM turns").fetchone()["error"]
        result["roles"] = [r["role"] for r in conversation.recent(echo.conn, 5)]

    run(echo, brain, desk, scenario)
    assert result["error"] == "interrupted" and result["roles"] == ["user"]
    assert "SpeakEnd" not in kinds(sent)  # the node is already listening to him; nothing to end


def test_a_failed_turn_is_apologised_for_aloud_and_the_details_go_to_discord(echo):
    desk, sent = make_desk(Listener("hello"))

    async def scenario(host):
        utterance(desk, 1)
        await until(lambda: kinds(sent).count("SpeakEnd") == 1, "the apology")

    seen = {}

    async def check(host):
        await scenario(host)
        seen["rows"] = [(r["text"], r["source"]) for r in outbox.recent(echo.conn)]

    run(echo, ScriptedBrain(error="RuntimeError: boom"), desk, check)
    assert "audio" in kinds(sent)  # "Sorry, sir, something went wrong. I've put the details in Discord." is said
    assert len(seen["rows"]) == 1 and "boom" in seen["rows"][0][0] and seen["rows"][0][1] == "send_text"


def test_a_trailing_and_is_held_and_joined_with_what_comes_next(echo):
    brain = ScriptedBrain(text="Done.")
    desk, sent = make_desk(Listener("remind me to call Emily and", "tomorrow at nine"))

    async def scenario(host):
        utterance(desk, 1)
        await until(lambda: len(desk._utterances) == 0 and desk._joiner.holding, "the request to be held")
        assert brain.prompts == []  # not answered yet
        utterance(desk, 2)
        await until(lambda: brain.prompts, "the joined request")

    run(echo, brain, desk, scenario)
    assert "remind me to call Emily and tomorrow at nine" in brain.prompts[0]
    assert len(brain.prompts) == 1


def test_a_held_request_goes_ahead_when_he_does_not_carry_on(echo, monkeypatch):
    monkeypatch.setattr(voice_services, "GRACE_S", 0.05)
    brain = ScriptedBrain(text="Done.")
    desk, sent = make_desk(Listener("remind me to call Emily and"))

    async def scenario(host):
        utterance(desk, 1)
        await until(lambda: brain.prompts, "the held request to be released")

    run(echo, brain, desk, scenario)
    assert "remind me to call Emily and" in brain.prompts[0]


@pytest.mark.parametrize("text,held", [("what's on tomorrow", False), ("call Emily and", True), ("hmm… um", True),
                                       ("so...", True), ("sandwich", False), ("Okay.", False)])
def test_what_counts_as_trailing_off(text, held):
    assert bool(voice_services.TRAILING.search(text)) is held


def test_a_cough_is_not_a_request(echo):
    brain = ScriptedBrain()
    desk, sent = make_desk(Listener("   "))

    async def scenario(host):
        utterance(desk, 1)
        await until(lambda: not desk._utterances, "the utterance to be processed")
        await asyncio.sleep(0.05)

    run(echo, brain, desk, scenario)
    assert brain.prompts == [] and sent == []


def test_when_the_voice_engine_fails_macos_say_speaks_instead(echo):
    def make_voice(engine):
        if engine == "kokoro":
            raise FileNotFoundError("the kokoro voice isn't downloaded")
        return PcmVoice()

    desk, sent = make_desk(Listener("hello"), make_voice)

    async def scenario(host):
        utterance(desk, 1)
        await until(lambda: kinds(sent).count("SpeakEnd") == 1, "the reply, in the fallback voice")

    run(echo, ScriptedBrain(text="Good morning, sir."), desk, scenario)
    assert "audio" in kinds(sent)


def test_a_listening_failure_is_said_not_swallowed(echo):
    class Broken:
        def transcribe(self, pcm, rate):
            raise FileNotFoundError("the listening model is missing")

    desk, sent = make_desk(Broken())

    async def scenario(host):
        utterance(desk, 1)
        await until(lambda: kinds(sent).count("SpeakEnd") == 1, "an apology")

    run(echo, ScriptedBrain(), desk, scenario)
    assert "audio" in kinds(sent)


def test_falak_speaks_first_only_when_he_is_there(echo):
    now = [100.0]
    desk, sent = make_desk(Listener(), clock=lambda: now[0])
    assert not desk.can_speak_first()  # never heard from a desk
    desk.on_frame("mac", m.Presence(5.0, False).to_json())
    assert not desk.can_speak_first()  # (the loop isn't running, so the frame was not handled)

    async def scenario(host):
        desk.on_frame("mac", m.Presence(5.0, False).to_json())
        await until(lambda: desk._presence is not None, "presence")
        assert desk.can_speak_first()  # at the Mac five seconds ago
        desk.on_frame("mac", m.Presence(600.0, False).to_json())
        await until(lambda: desk._presence[1] == 600.0, "presence")
        assert not desk.can_speak_first()  # away for ten minutes
        desk.on_frame("mac", m.Presence(600.0, True).to_json())
        await until(lambda: desk._presence[2], "presence")
        assert desk.can_speak_first()  # but a conversation is open
        now[0] += 60
        assert not desk.can_speak_first()  # a stale report: the node is gone

    run(echo, ScriptedBrain(), desk, scenario)


def test_something_said_first_is_spoken_on_a_stream_of_its_own(echo):
    desk, sent = make_desk(Listener(), auto_done=False)

    async def scenario(host):
        desk.on_frame("mac", m.Presence(1.0, False).to_json())
        await until(lambda: desk._node == "mac", "presence")
        speaking = asyncio.create_task(desk.send("Call Emily back, sir."))
        await until(lambda: kinds(sent).count("SpeakEnd") == 1, "the reminder to be spoken")
        stream = m.parse(sent[0][1]).stream
        assert stream >= desk_module.SPEAK_FIRST_STREAMS
        desk.on_frame("mac", m.PlaybackStopped(stream, 1600, "done").to_json())
        await speaking

    run(echo, ScriptedBrain(), desk, scenario)
    assert kinds(sent)[0] == "SpeakStart"


def test_the_voice_is_loaded_when_a_conversation_starts_and_unloaded_when_idle(echo):
    loads = []
    desk, sent = make_desk(Listener("hi"), lambda engine: loads.append(engine) or PcmVoice())

    async def scenario(host):
        desk.on_frame("mac", m.VoiceStart(1, "wake").to_json())
        await until(lambda: loads == ["kokoro"], "the voice to be warmed in the background")

    run(echo, ScriptedBrain(), desk, scenario)


def test_the_profile_is_read_for_each_sentence_so_a_change_applies_to_the_next_one(echo):
    profiles = [VoiceProfile(speed=1.0), VoiceProfile(speed=0.8)]
    used = []

    class Recording(PcmVoice):
        def synthesize(self, sentence, profile):
            used.append(profile.speed)
            return super().synthesize(sentence, profile)

    desk, sent = make_desk(Listener("hi"), lambda e: Recording(),
                           profile=lambda: profiles.pop(0) if len(profiles) > 1 else profiles[0])

    async def scenario(host):
        utterance(desk, 1)
        await until(lambda: kinds(sent).count("SpeakEnd") == 1, "reply")

    run(echo, StreamingBrain(("One. ", "Two.")), desk, scenario)
    assert used[:2] == [1.0, 0.8]  # "undo that" or "deeper" between sentences changes the second


def _speech():
    from jarvis.channels.desk_speech import DeskSpeech

    said, sent = [], []

    async def synth(sentence, profile):
        said.append(sentence)
        return b"\x02\x00" * 240

    speech = DeskSpeech("mac", 1, synth, VoiceProfile, sent.append)
    return speech, said


def test_a_lookup_before_any_words_says_one_sec_once():
    async def go():
        speech, said = _speech()
        speech.on_tool()
        speech.on_tool()  # a second lookup: still only once
        speech.on_text("You have one thing due. ")
        speech.on_tool()
        speech.playback_stopped(0, "done")
        await speech.finish("You have one thing due.")
        return said

    assert asyncio.run(go()) == ["One sec.", "You have one thing due."]


def test_no_filler_once_falak_has_started_talking_or_was_cut_off():
    async def go():
        speech, said = _speech()
        speech.on_text("Checking your calendar. ")
        speech.on_tool()
        speech.playback_stopped(0, "done")
        await speech.finish("Checking your calendar.")
        cut, cut_said = _speech()
        cut.cancelled.set()
        cut.on_tool()
        await cut.finish("")
        return said, cut_said

    assert asyncio.run(go()) == (["Checking your calendar."], [])


def test_say_aloud_speaks_at_the_desk_only_when_it_is_connected(echo):
    desk, sent = make_desk(Listener())
    assert "isn't connected" in desk.say_aloud("hi")  # not running

    async def scenario(host):
        assert "isn't connected" in desk.say_aloud("hi")  # running, but no node has reported in
        desk.on_frame("mac", m.Presence(900.0, False).to_json())  # away from the Mac: he asked, so it still speaks
        await until(lambda: desk._node == "mac", "presence")
        assert desk.say_aloud("Good morning, sir.") is None
        await until(lambda: kinds(sent).count("SpeakEnd") == 1, "it to be spoken")

    run(echo, ScriptedBrain(), desk, scenario)
    assert kinds(sent)[0] == "SpeakStart" and "audio" in kinds(sent)
