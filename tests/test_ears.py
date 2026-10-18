import threading

import numpy as np
import pytest

from jarvis import config
from jarvis.ears import CHIME, CHUNK, LISTENING, MISS, RATE, SPEAKING, Ears, Endpointer, rms

STEP = CHUNK / RATE


def tone(level: float) -> np.ndarray:
    return (np.ones(CHUNK) * level * 32768).astype(np.int16)


SILENT, LOUD = tone(0.001), tone(0.1)


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setattr(config, "EAR_END_SILENCE_SECONDS", 1.2)
    monkeypatch.setattr(config, "EAR_MAX_SILENCE_SECONDS", 1.5)
    monkeypatch.setattr(config, "BARGE_IN_MIN_MS", 300)
    monkeypatch.setattr(config, "BARGE_IN_LEVEL", 3.0)
    # The fake mic below has no clock, so a barge-in watch would race the follow-up turn;
    # the barge-in tests turn it back on with a mic paced by the speech.
    monkeypatch.setattr(config, "BARGE_IN", False)


def test_endpointer_waits_starts_and_ends():
    ep = Endpointer(noise_floor=0.002, start_timeout=1.0)
    assert ep.feed(0.001, STEP) == "waiting"
    assert ep.feed(0.05, STEP) == "speaking"
    states = [ep.feed(0.001, STEP) for _ in range(15)]  # 1.2 s of quiet
    assert states[-1] == "done" and "speaking" in states[:-1]

    never = Endpointer(0.002, start_timeout=0.5)
    assert [never.feed(0.001, STEP) for _ in range(7)][-1] == "nothing"


def test_a_mid_sentence_pause_keeps_the_turn_and_a_long_one_ends_it():
    ep = Endpointer(noise_floor=0.002)
    for _ in range(8):
        ep.feed(0.05, STEP)
    assert {ep.feed(0.001, STEP) for _ in range(10)} == {"speaking"}  # 0.8 s gap: still one turn
    for _ in range(5):
        assert ep.feed(0.05, STEP) == "speaking"
    # he paused and carried on, so the next pause gets the longer window (1.5 s)
    states = [ep.feed(0.001, STEP) for _ in range(19)]
    assert states[:15] == ["speaking"] * 15 and states[-1] == "done"


def test_quieter_trailing_words_still_count_as_speech():
    ep = Endpointer(noise_floor=0.002)
    ep.feed(0.05, STEP)
    assert ep.threshold > 0.007 > ep.hold  # too quiet to start a turn, loud enough to continue one
    for _ in range(30):
        assert ep.feed(0.007, STEP) == "speaking"


def test_barge_in_needs_sustained_speech_over_the_raised_threshold():
    ep = Endpointer(noise_floor=0.002, phase=SPEAKING)
    assert ep.feed(0.015, STEP) == "talking"  # normal speech level, but JARVIS is talking: not enough
    assert [ep.feed(0.1, STEP) for _ in range(3)] == ["talking"] * 3  # 240 ms: a cough, not a barge-in
    ep.feed(0.001, STEP)
    states = [ep.feed(0.1, STEP) for _ in range(4)]  # 320 ms sustained
    assert states[-1] == "barge_in" and ep.phase == LISTENING
    assert ep.feed(0.001, STEP) == "speaking"  # now it's an ordinary turn


def test_jarvis_own_echo_does_not_barge_in():
    ep = Endpointer(noise_floor=0.002, phase=SPEAKING)
    echo = [0.05, 0.06, 0.04, 0.07, 0.05, 0.03, 0.06] * 10  # its voice through the speakers, loud and steady
    assert {ep.feed(level, STEP, playing=True) for level in echo} == {"talking"}
    assert [ep.feed(0.2, STEP, playing=True) for _ in range(4)][-1] == "barge_in"  # him, clearly over the echo


def test_endpointer_threshold_tracks_noisy_rooms():
    noisy = Endpointer(noise_floor=0.02)
    assert noisy.feed(0.04, STEP) == "waiting"  # below 3x noise floor: not speech
    assert noisy.feed(0.07, STEP) == "speaking"


def test_rms():
    assert rms(SILENT) < 0.002 and 0.09 < rms(LOUD) < 0.11


class FakeStream:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.read_available = 0

    def read(self, n):
        chunk = self.chunks.pop(0) if self.chunks else SILENT
        return chunk.reshape(-1, 1), False


class FakeVoice:
    """Stands in for SpeakQueue: records what would be said, in order."""

    def __init__(self, events=None):
        self.events = events if events is not None else []
        self.first_audio_at = None

    def say(self, text):
        self.events.append(f"say:{text}")
        self.first_audio_at = self.first_audio_at or 100.0

    def cancel(self):
        self.events.append("cancel")

    def reset(self):
        self.first_audio_at = None

    def wait(self):
        self.events.append("waited")


def spoken_of(voice):
    return [e[4:] for e in voice.events if e.startswith("say:")]


def make(chunks, heard="what's due this week", monkeypatch=None):
    played, answered, voice = [], [], FakeVoice()
    ears = Ears(answer=lambda t, on_text=None: answered.append(t) or "Econ on Sunday, sir.",
                transcribe=lambda audio: heard, log=lambda s: None, play=played.append, voice=voice)
    ears.noise.extend([0.002] * 10)
    return ears, FakeStream(chunks), played, voice, answered


def test_conversation_answers_then_waits_for_follow_up(monkeypatch):
    monkeypatch.setattr(config, "FOLLOW_UP_SECONDS", 1)
    speech = [LOUD] * 10 + [SILENT] * 14
    ears, stream, played, voice, answered = make(speech)
    ears.converse(stream)
    assert played == [CHIME]
    assert answered == ["what's due this week"] and spoken_of(voice) == ["Econ on Sunday, sir."]


def test_follow_up_without_wake_word(monkeypatch):
    monkeypatch.setattr(config, "FOLLOW_UP_SECONDS", 2)
    turn = [LOUD] * 8 + [SILENT] * 14
    ears, stream, played, voice, answered = make(turn + [SILENT] * 3 + turn)
    ears.converse(stream)
    assert len(answered) == 2 and len(spoken_of(voice)) == 2


def test_each_sentence_is_spoken_as_soon_as_it_streams(monkeypatch):
    monkeypatch.setattr(config, "FOLLOW_UP_SECONDS", 1)
    events = []

    def answer(text, on_text=None):
        for partial in ["Two", "Two things, sir.", "Two things, sir. Econ on Sunday.",
                        "Two things, sir. Econ on Sunday. English"]:
            on_text(partial)
            events.append(f"streamed:{len(partial)}")
        return "Two things, sir. Econ on Sunday. English on Monday."

    timings = []
    ears = Ears(answer=answer, transcribe=lambda a: "what's due", log=lambda s: None, play=lambda p: None,
                voice=FakeVoice(events), timing=timings.append)
    ears.noise.extend([0.002] * 10)
    ears.converse(FakeStream([LOUD] * 8 + [SILENT] * 14))
    said = [e for e in events if e.startswith(("say:", "waited", "streamed:"))]
    assert said == ["streamed:3", "streamed:16", "say:Two things, sir.", "streamed:32",
                    "say:Econ on Sunday.", "streamed:40", "say:English on Monday.", "waited"]
    assert len(timings) == 1  # time to first audio was measured


def test_reply_that_doesnt_match_the_stream_replaces_it():
    events = []

    def answer(text, on_text=None):
        on_text("Checking, sir. ")
        return "Sorry, I couldn't think just now (timeout)."

    ears = Ears(answer=answer, transcribe=lambda a: "x", log=lambda s: None, play=lambda p: None,
                voice=FakeVoice(events))
    ears.reply("x", heard_at=99.0)
    assert events[events.index("say:Checking, sir."):] == [
        "say:Checking, sir.", "cancel", "say:Sorry, I couldn't think just now (timeout).", "waited"]


class PlayingVoice(FakeVoice):
    """A SpeakQueue stand-in that 'plays' from its first sentence until cancelled (or ~0.5 s)."""

    def __init__(self, events=None):
        super().__init__(events)
        self.playing, self.started, self.cut = False, threading.Event(), threading.Event()

    def say(self, text):
        super().say(text)
        self.playing = True
        self.started.set()

    def cancel(self):
        if self.playing:  # only a cut-off counts; the reset at the start of each reply doesn't
            self.events.append("cancel")
            self.playing = False
            self.cut.set()

    def reset(self):
        super().reset()
        self.started.clear()
        self.cut.clear()

    def wait(self):
        self.cut.wait(0.5)
        self.playing = False
        self.events.append("waited")


class DeskMic(FakeStream):
    """The first turn is there at once; what comes while JARVIS answers only arrives once it
    has started talking, like a real mic paced by time."""

    def __init__(self, first, during, voice):
        super().__init__(first)
        self.during, self.voice = list(during), voice

    def read(self, n):
        if not self.chunks and self.during:
            self.voice.started.wait(2)
            self.chunks, self.during = self.during, []
        return super().read(n)


ECHO = [tone(level) for level in (0.03, 0.04, 0.03, 0.05, 0.04)] * 6  # JARVIS heard through the speakers


def desk(during, answers, monkeypatch):
    monkeypatch.setattr(config, "BARGE_IN", True)
    monkeypatch.setattr(config, "FOLLOW_UP_SECONDS", 1)
    voice, heard, answered = PlayingVoice(), [], []

    def answer(text, on_text=None):
        answered.append(text)
        return answers[len(answered) - 1](on_text, voice)

    def transcribe(audio):
        loud = sum(rms(audio[i:i + CHUNK] * 32768) > 0.05 for i in range(0, audio.size, CHUNK))
        heard.append(loud)
        return f"turn {len(heard)}"

    ears = Ears(answer=answer, transcribe=transcribe, log=lambda s: None, play=lambda p: None, voice=voice)
    ears.noise.extend([0.002] * 10)
    ears.converse(DeskMic([LOUD] * 8 + [SILENT] * 16, during, voice))
    return voice, heard, answered


def long_answer(on_text, voice):
    on_text("Well, sir, there are three things. ")
    voice.cut.wait(2)  # still writing when he cuts in
    on_text("Well, sir, there are three things. Econ is first. ")
    return "Well, sir, there are three things. Econ is first. English is second."


def short_answer(on_text, voice):
    return "Right away, sir."


def test_talking_over_jarvis_stops_it_and_answers_what_he_said(monkeypatch):
    # he cuts in, pauses mid-sentence for 0.8 s, then finishes the thought
    during = ECHO[:6] + [LOUD] * 6 + [SILENT] * 10 + [LOUD] * 5
    voice, heard, answered = desk(during, [long_answer, short_answer], monkeypatch)
    assert spoken_of(voice) == ["Well, sir, there are three things.", "Right away, sir."]
    assert "cancel" in voice.events  # cut off mid-sentence, the rest dropped
    assert answered == ["turn 1", "turn 2"]  # no wake word needed for the interruption
    assert heard == [8, 11]  # every word of the interruption, both sides of the pause


def test_jarvis_hearing_its_own_voice_does_not_interrupt_itself(monkeypatch):
    def two_sentences(on_text, voice):
        on_text("Econ on Sunday, sir. ")
        return "Econ on Sunday, sir. English on Monday."

    voice, heard, answered = desk(ECHO, [two_sentences], monkeypatch)
    assert spoken_of(voice) == ["Econ on Sunday, sir.", "English on Monday."]
    assert "cancel" not in voice.events and answered == ["turn 1"]


def test_barge_in_off_keeps_the_mic_closed_while_talking(monkeypatch):
    during = ECHO[:6] + [LOUD] * 6 + [SILENT] * 20
    monkeypatch.setattr(config, "BARGE_IN", False)
    voice, events = PlayingVoice(), []
    ears = Ears(answer=lambda t, on_text=None: "Fine, sir.", transcribe=lambda a: "x", log=events.append,
                play=lambda p: None, voice=voice)
    stream = DeskMic([], during, voice)
    ears.reply("x", heard_at=1.0, read_chunk=lambda: stream.read(CHUNK)[0][:, 0])
    assert ears.barged is None and "cancel" not in voice.events and len(stream.during) == len(during)


def test_wake_with_no_speech_or_garbled_speech_pops():
    ears, stream, played, _, answered = make([SILENT] * 80)
    ears.converse(stream)
    assert played == [CHIME, MISS] and answered == []

    ears2, stream2, played2, _, answered2 = make([LOUD] * 8 + [SILENT] * 14, heard="")
    ears2.converse(stream2)
    assert played2 == [CHIME, MISS] and answered2 == []
