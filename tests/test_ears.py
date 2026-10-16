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


def make(chunks, heard="what's due this week", monkeypatch=None):
    played, spoken, answered = [], [], []
    ears = Ears(answer=lambda t, on_first=None: answered.append(t) or "Econ on Sunday, sir.",
                transcribe=lambda audio: heard, log=lambda s: None,
                speak=spoken.append, play=played.append)
    ears.noise.extend([0.002] * 10)
    return ears, FakeStream(chunks), played, spoken, answered


def test_conversation_answers_then_waits_for_follow_up(monkeypatch):
    monkeypatch.setattr(config, "FOLLOW_UP_SECONDS", 1)
    speech = [LOUD] * 10 + [SILENT] * 14
    ears, stream, played, spoken, answered = make(speech)
    ears.converse(stream)
    assert played == [CHIME]
    assert answered == ["what's due this week"] and spoken == ["Econ on Sunday, sir."]


def test_follow_up_without_wake_word(monkeypatch):
    monkeypatch.setattr(config, "FOLLOW_UP_SECONDS", 2)
    turn = [LOUD] * 8 + [SILENT] * 14
    ears, stream, played, spoken, answered = make(turn + [SILENT] * 3 + turn)
    ears.converse(stream)
    assert len(answered) == 2 and len(spoken) == 2


class FakeSpeech:
    def __init__(self, log):
        self.log = log

    def wait(self):
        self.log.append("waited")


def test_first_sentence_spoken_early_then_the_rest(monkeypatch):
    monkeypatch.setattr(config, "FOLLOW_UP_SECONDS", 1)
    events = []

    def answer(text, on_first=None):
        on_first("Two things, sir.")
        events.append("answer done")
        return "Two things, sir. Econ on Sunday and English on Monday."

    ears = Ears(answer=answer, transcribe=lambda a: "what's due", log=lambda s: None,
                speak=lambda t: events.append(f"say:{t}"), play=lambda p: None,
                speak_async=lambda t: events.append(f"early:{t}") or FakeSpeech(events))
    ears.noise.extend([0.002] * 10)
    ears.converse(FakeStream([LOUD] * 8 + [SILENT] * 14))
    assert events == ["early:Two things, sir.", "answer done", "waited",
                      "say:Econ on Sunday and English on Monday."]


def test_wake_with_no_speech_or_garbled_speech_pops():
    ears, stream, played, spoken, answered = make([SILENT] * 80)
    ears.converse(stream)
    assert played == [CHIME, MISS] and answered == []

    ears2, stream2, played2, _, answered2 = make([LOUD] * 8 + [SILENT] * 14, heard="")
    ears2.converse(stream2)
    assert played2 == [CHIME, MISS] and answered2 == []
