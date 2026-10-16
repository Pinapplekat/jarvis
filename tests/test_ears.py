import numpy as np

from jarvis import config
from jarvis.ears import CHIME, CHUNK, MISS, RATE, Ears, Endpointer, rms

STEP = CHUNK / RATE


def tone(level: float) -> np.ndarray:
    return (np.ones(CHUNK) * level * 32768).astype(np.int16)


SILENT, LOUD = tone(0.001), tone(0.1)


def test_endpointer_waits_starts_and_ends():
    ep = Endpointer(noise_floor=0.002, start_timeout=1.0)
    assert ep.feed(0.001, STEP) == "waiting"
    assert ep.feed(0.05, STEP) == "speaking"
    states = [ep.feed(0.001, STEP) for _ in range(13)]  # ~1.04 s of quiet
    assert states[-1] == "done" and "speaking" in states[:-1]

    never = Endpointer(0.002, start_timeout=0.5)
    assert [never.feed(0.001, STEP) for _ in range(7)][-1] == "nothing"


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


def test_wake_with_no_speech_or_garbled_speech_pops():
    ears, stream, played, _, answered = make([SILENT] * 80)
    ears.converse(stream)
    assert played == [CHIME, MISS] and answered == []

    ears2, stream2, played2, _, answered2 = make([LOUD] * 8 + [SILENT] * 14, heard="")
    ears2.converse(stream2)
    assert played2 == [CHIME, MISS] and answered2 == []
