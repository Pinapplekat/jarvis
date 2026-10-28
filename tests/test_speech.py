"""The speech engines without any model: the sentence splitter, profiles and the one-engine-at-a-time manager."""

import numpy as np

from jarvis.speech import SentenceBuffer, VoiceManager, VoiceProfile, sentences
from jarvis.speech.listener import WhisperListener, to_16k_float
from jarvis.speech.models import find, hf_snapshot


def cut(text):
    return list(sentences([text]))


def test_cuts_at_sentence_ends():
    assert cut("You have two meetings. The first is at nine! Want the rest?") == [
        "You have two meetings.", "The first is at nine!", "Want the rest?"]


def test_never_cuts_inside_an_abbreviation_a_number_a_url_or_an_initial():
    assert cut("Dr. Okafor is at 3.5 miles, e.g. near example.com/a.b today. Then J. Smith leaves.") == [
        "Dr. Okafor is at 3.5 miles, e.g. near example.com/a.b today.", "Then J. Smith leaves."]
    assert cut("See https://example.com/a.html now. Next.") == ["See https://example.com/a.html now.", "Next."]
    assert cut("It starts at 5 p.m. today. Fine.") == ["It starts at 5 p.m. today.", "Fine."]


def test_never_cuts_inside_quotes():
    assert cut('The file is called "Notes. Final" and it is open. Done.') == [
        'The file is called "Notes. Final" and it is open.', "Done."]
    assert cut('He said "no." Then he left.') == ['He said "no."', "Then he left."]


def test_streaming_gives_a_sentence_as_soon_as_it_is_complete():
    buf = SentenceBuffer()
    assert buf.feed("One sec, checking") == []
    assert buf.feed(" your calendar. You") == ["One sec, checking your calendar."]
    assert buf.feed(" have two things.") == []  # nothing follows the period yet: it could be "3.5"
    assert buf.feed(" Dinner") == ["You have two things."]
    assert buf.flush() == ["Dinner"]
    assert buf.flush() == []


def test_a_stream_of_small_deltas_matches_the_whole_text():
    text = "Dr. Who is at 3.5 miles. Ready, sir?\nYes."
    assert list(sentences(iter(text))) == cut(text) == ["Dr. Who is at 3.5 miles.", "Ready, sir?", "Yes."]


def test_voice_profile_roundtrips_and_designs_an_omnivoice_string():
    p = VoiceProfile(gender="male", pitch="low pitch", accent="british accent")
    assert p.instruct() == "male, low pitch, british accent"
    assert VoiceProfile(whisper=True).instruct() == "whisper"
    assert VoiceProfile.from_dict({**p.to_dict(), "stale_field": 1}) == p


class FakeVoice:
    def __init__(self, engine, log):
        self.engine, self.log = engine, log
        log.append(f"load {engine}")

    def synthesize(self, sentence, profile):
        return sentence.encode()

    def unload(self):
        self.log.append(f"unload {self.engine}")


def test_one_engine_at_a_time_unloaded_when_idle_and_loaded_again():
    log, now = [], [0.0]
    mgr = VoiceManager(lambda e: FakeVoice(e, log), idle_minutes=30, clock=lambda: now[0])
    profile = VoiceProfile()
    assert mgr.synthesize("kokoro", "hi", profile) == b"hi"
    mgr.synthesize("kokoro", "again", profile)
    assert log == ["load kokoro"] and mgr.loaded == "kokoro"
    mgr.synthesize("say", "swap", profile)
    assert log == ["load kokoro", "unload kokoro", "load say"]  # never two at once
    now[0] = 29 * 60
    assert not mgr.tick() and mgr.loaded == "say"
    now[0] = 31 * 60
    assert mgr.tick() and mgr.loaded == ""
    mgr.warm("say", profile)
    assert log[-2:] == ["unload say", "load say"] and mgr.loaded == "say"


def test_resampling_and_clicks():
    pcm = (np.sin(np.linspace(0, 100, 24000)) * 8000).astype("<i2").tobytes()  # 1 s at 24 kHz
    assert abs(len(to_16k_float(pcm, 24000)) - 16000) <= 1
    assert len(to_16k_float(pcm, 16000)) == 24000
    assert WhisperListener(None).transcribe(b"\0" * 400, 16000) == ""  # a click never loads the model


def test_finds_models_in_a_cache_folder_without_touching_it(tmp_path):
    snap = tmp_path / "models--Systran--faster-whisper-base.en" / "snapshots" / "abc"
    snap.mkdir(parents=True)
    (tmp_path / "kokoro-mlx").mkdir()
    assert hf_snapshot(tmp_path, "Systran/faster-whisper-base.en") == snap
    assert find([tmp_path / "nope", tmp_path], "Systran/faster-whisper-base.en") == snap
    assert find([tmp_path], "x/Kokoro", folder="kokoro-mlx") == tmp_path / "kokoro-mlx"
    assert find([tmp_path], "x/missing") is None
    assert sorted(p.name for p in tmp_path.iterdir()) == ["kokoro-mlx", "models--Systran--faster-whisper-base.en"]
