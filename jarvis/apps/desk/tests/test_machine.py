"""The desk's conversation: wake word -> listening -> waiting -> speaking -> follow-up -> idle, push-to-talk from any
state, and barge-in from speaking and waiting. Pure: no audio, no clock."""

from jarvis.apps.desk.machine import FOLLOW_UP, IDLE, LISTENING, SPEAKING, WAITING, DeskMachine, Timing
from jarvis.protocol.tracker import SpeechTracker

T = Timing()


def kinds(effects):
    return [e[0] for e in effects]


def listening(m, now=0.0, source="wake"):
    out = m.wake(now) if source == "wake" else m.push_down(now)
    assert m.state == LISTENING
    return out


def talk_and_stop(m, now):
    m.vad(now, 400, 0)  # he talks...
    return m.vad(now + 0.8, 0, T.end_silence_ms)  # ...and is quiet long enough


def waiting(m):
    listening(m)
    talk_and_stop(m, 1.0)
    assert m.state == WAITING


def speaking(m, stream=1, now=2.0):
    waiting(m)
    m.host_speak_start(now, stream)
    assert m.state == SPEAKING


def test_the_whole_conversation_in_order():
    m = DeskMachine()
    assert m.state == IDLE
    assert m.wake(0.0) == [("chime", "start"), ("voice_start", 1, "wake"), ("stream_mic", True)]
    assert m.vad(0.5, 300, 0) == []  # talking
    assert m.vad(1.0, 0, 400) == []  # a pause shorter than end_silence_ms
    assert m.vad(1.4, 0, 700) == [("stream_mic", False), ("voice_end", 1)] and m.state == WAITING
    assert m.host_speak_start(2.0, 1) == [] and m.state == SPEAKING
    assert m.host_speak_end(3.0, 1) == [] and m.state == SPEAKING  # audio is still playing
    assert m.playback_idle(4.0) == [("playback_stopped", 1, "done")] and m.state == FOLLOW_UP
    assert m.tick(10.0) == [] and m.state == FOLLOW_UP
    assert m.tick(4.0 + 15.0) == [("chime", "end")] and m.state == IDLE


def test_a_follow_up_needs_no_wake_word_and_no_chime():
    m = DeskMachine()
    speaking(m)
    m.host_speak_end(3.0, 1)
    m.playback_idle(4.0)
    assert m.vad(6.0, 100, 0) == []  # a cough: not enough speech yet
    out = m.vad(6.2, 200, 0)
    assert out == [("voice_start", 2, "follow_up"), ("stream_mic", True)] and m.state == LISTENING


def test_hands_on_the_wake_word_are_ignored_while_speaking_or_waiting():
    m = DeskMachine()
    waiting(m)
    assert m.wake(3.0) == [] and m.state == WAITING
    m.host_speak_start(3.0, 1)
    assert m.wake(3.1) == [] and m.state == SPEAKING


def test_speech_underruns_do_not_end_the_reply():
    m = DeskMachine()
    speaking(m)
    assert m.playback_idle(3.0) == [] and m.state == SPEAKING  # sentence 1 is played, sentence 2 is still being made
    m.playback_busy()
    assert m.host_speak_end(4.0, 1) == [] and m.state == SPEAKING
    assert m.playback_idle(5.0) == [("playback_stopped", 1, "done")] and m.state == FOLLOW_UP


def test_barge_in_while_speaking_stops_the_speakers_here_then_listens():
    m = DeskMachine()
    speaking(m, now=2.0)
    assert m.vad(2.1, 300, 0) == []  # inside the echo warm-up: Falak's own first syllable
    assert m.vad(2.5, 200, 0) == []  # not long enough yet
    out = m.vad(2.6, 260, 0)
    assert out == [("stop_playback", "barge_in"), ("playback_stopped", 1, "barge_in"),
                   ("voice_start", 2, "follow_up"), ("stream_mic", True)]
    assert m.state == LISTENING


def test_barge_in_while_waiting_has_nothing_to_stop():
    m = DeskMachine()
    waiting(m)
    out = m.vad(3.0, 260, 0)
    assert out == [("playback_stopped", 1, "barge_in"), ("voice_start", 2, "follow_up"), ("stream_mic", True)]


def test_a_reply_to_something_he_talked_over_is_not_played():
    m = DeskMachine()
    waiting(m)
    m.vad(3.0, 260, 0)  # barge-in from waiting; now listening
    assert m.host_speak_start(3.5, 1) == [] and m.state == LISTENING


def test_push_to_talk_works_from_every_state():
    for setup, expects_stop in ((lambda m: None, False), (waiting, False), (speaking, True)):
        m = DeskMachine()
        setup(m)
        out = m.push_down(5.0)
        assert m.state == LISTENING and ("voice_start", m.stream, "push") in out and ("chime", "start") in out
        assert (("stop_playback", "barge_in") in out) is expects_stop
    m = DeskMachine()
    speaking(m)
    m.host_speak_end(3.0, 1)
    m.playback_idle(4.0)
    assert m.state == FOLLOW_UP and kinds(m.push_down(5.0))[:2] == ["chime", "voice_start"]


def test_hold_to_talk_ends_on_release_and_a_tap_starts_a_conversation():
    m = DeskMachine()
    m.push_down(0.0)
    assert m.vad(1.0, 0, 3000) == []  # held: silence does not end it
    assert m.push_up(2.0) == [("stream_mic", False), ("voice_end", 1)] and m.state == WAITING
    m = DeskMachine()
    m.push_down(0.0)
    assert m.push_up(0.1) == [] and m.state == LISTENING  # a tap: stays open
    m.vad(0.5, 300, 0)
    assert kinds(m.vad(1.4, 0, 700)) == ["stream_mic", "voice_end"]


def test_nothing_said_after_the_wake_word_goes_back_to_idle_with_an_end_chime():
    m = DeskMachine()
    listening(m)
    assert m.vad(5.0, 0, 5000) == []
    assert m.vad(6.0, 0, 6000) == [("stream_mic", False), ("voice_end", 1), ("chime", "end")] and m.state == IDLE


def test_the_host_answering_with_nothing_or_never_answering_goes_to_follow_up():
    m = DeskMachine()
    waiting(m)
    assert m.host_speak_end(3.0, 1) == [] and m.state == FOLLOW_UP
    m = DeskMachine()
    waiting(m)
    assert m.tick(1.8 + 44) == [] and m.state == WAITING
    m.tick(1.8 + 46)
    assert m.state == FOLLOW_UP


def test_a_stop_from_the_host_cuts_the_audio_and_opens_a_follow_up():
    m = DeskMachine()
    speaking(m)
    assert m.host_stop(3.0, 1) == [("stop_playback", "stop"), ("playback_stopped", 1, "stop")] and m.state == FOLLOW_UP


def test_speaking_first_goes_back_to_idle_and_never_opens_a_follow_up():
    # In class, a notice Falak said on its own opened a follow-up window, and people talking nearby became turns
    # (2026-10-28). Only a conversation he started gets follow-ups.
    m = DeskMachine()
    m.host_speak_start(1.0, 2**31)
    assert m.state == SPEAKING and m.reply_stream == 2**31
    assert m.vad(1.6, 900, 0) == []  # someone nearby talking over it doesn't cut in either
    m.host_speak_end(2.0, 2**31)
    assert m.playback_idle(3.0) == [("playback_stopped", 2**31, "done")] and m.state == IDLE
    assert m.vad(4.0, 900, 0) == [] and m.state == IDLE  # and nothing nearby starts a turn afterwards
    assert ("voice_start", 1, "push") in m.push_down(5.0)  # the key still works


def test_a_reply_to_him_still_opens_a_follow_up():
    m = DeskMachine()
    speaking(m, now=2.0)
    m.host_speak_end(3.0, 1)
    m.playback_idle(3.5)
    assert m.state == FOLLOW_UP


def test_the_tracker_counts_speech_and_silence_and_forgives_a_breath():
    t = SpeechTracker(threshold=0.5, hangover_ms=150)
    assert t.feed(0.9) == (30, 0) and t.feed(0.9) == (60, 0)
    assert t.feed(0.1) == (60, 30) and t.feed(0.1) == (60, 60)  # a short gap: still one stretch of speech
    assert t.feed(0.9) == (90, 0)
    for _ in range(5):
        speech, silence = t.feed(0.1)
    assert (speech, silence) == (0, 150)  # a long gap ends it, and the silence keeps counting
    assert t.feed(0.1) == (0, 180)
    t.reset()
    assert t.feed(0.1) == (0, 30)


def test_without_echo_cancellation_its_own_voice_never_cuts_it_off_but_the_key_still_does():
    m = DeskMachine()
    m.voice_barge_in = False
    speaking(m, now=2.0)
    assert m.vad(3.0, 900, 0) == [] and m.state == SPEAKING  # that's Falak, heard through the speakers
    assert ("stop_playback", "barge_in") in m.push_down(3.1)
