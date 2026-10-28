"""The desk session with a fake helper, fake VAD and fake wake word: what goes to the host, what goes to the speakers."""

from jarvis.apps.desk.machine import FOLLOW_UP, IDLE, LISTENING, SPEAKING, WAITING, Timing
from jarvis.apps.desk.session import DeskSession
from jarvis.protocol import messages as m

FRAME = b"\x01\x00" * 480  # 30 ms of 16 kHz PCM16


class FakeHelper:
    def __init__(self):
        self.played, self.commands, self.closed = [], [], False

    def play(self, pcm):
        self.played.append(pcm)

    def command(self, cmd):
        self.commands.append(cmd)

    def close(self):
        self.closed = True


class Rig:
    """Drives a session by hand: `loud` and `quiet` frames set what the fake VAD says, `wake_now` fires the wake word."""

    def __init__(self, chimes=True, wake=True):
        self.helper, self.sent, self.now, self.prob, self.woke = FakeHelper(), [], 0.0, 0.0, False
        self.session = DeskSession(helper=self.helper, vad=lambda pcm: self.prob, wake=self._wake if wake else None,
                                   send=self.sent.append, timing=Timing(), chimes=chimes, clock=lambda: self.now,
                                   idle_seconds=lambda: 2.5)

    def _wake(self, chunk):
        woke, self.woke = self.woke, False
        return woke

    def frames(self, n, loud):
        self.prob = 0.9 if loud else 0.0
        for _ in range(n):
            self.now += 0.03
            self.session.handle("mic", FRAME)

    def host(self, frame):
        self.session.handle("host", frame.to_json() if hasattr(frame, "to_json") else frame)

    def messages(self):
        return [m.parse(f) if isinstance(f, str) else m.AudioFrame.from_bytes(f) for f in self.sent]

    def types(self):
        return [type(x).__name__ for x in self.messages()]


def say_hey_falak(rig):
    rig.woke = True
    rig.frames(3, loud=True)  # 90 ms: enough for the 80 ms wake chunk to be fed


def test_nothing_leaves_the_node_while_idle():
    rig = Rig()
    rig.frames(100, loud=True)  # a TV, a conversation in the room: no wake word, no push-to-talk
    assert rig.sent == [] and rig.session.state == IDLE


def test_wake_word_streams_his_voice_after_a_voice_start_and_ends_on_silence():
    rig = Rig()
    say_hey_falak(rig)
    assert rig.session.state == LISTENING
    assert rig.helper.commands == [{"cmd": "chime", "name": "start"}]
    assert rig.messages() == [m.VoiceStart(1, "wake")]  # the wake phrase itself is not sent
    rig.frames(10, loud=True)
    rig.frames(24, loud=False)  # 720 ms of quiet
    kinds = rig.types()
    assert kinds[0] == "VoiceStart" and kinds[-1] == "VoiceEnd" and set(kinds[1:-1]) == {"AudioFrame"}
    audio = [x for x in rig.messages() if isinstance(x, m.AudioFrame)]
    assert [a.seq for a in audio] == list(range(len(audio))) and {a.stream for a in audio} == {1}
    assert all(a.kind == m.MIC and a.pcm == FRAME for a in audio) and rig.session.state == WAITING
    count = len(rig.sent)
    rig.frames(20, loud=False)
    assert len(rig.sent) == count  # nothing streams while the host thinks


def test_the_first_word_is_not_lost_when_a_follow_up_starts():
    rig = Rig()
    rig.session.machine.state = FOLLOW_UP
    rig.frames(4, loud=False)
    rig.frames(7, loud=True)  # 210 ms of speech opens the turn
    msgs = rig.messages()
    assert msgs[0] == m.VoiceStart(1, "follow_up")
    audio = [x for x in msgs if isinstance(x, m.AudioFrame)]
    assert len(audio) >= 7  # the half second before it was certain, including the frames that came before


def run_a_turn_until_speaking(rig, stream=1):
    say_hey_falak(rig)
    rig.frames(10, loud=True)
    rig.frames(24, loud=False)
    rig.sent.clear()
    rig.host(m.SpeakStart(stream))
    return stream


def test_reply_audio_plays_and_the_end_of_it_opens_a_follow_up():
    rig = Rig()
    run_a_turn_until_speaking(rig)
    assert rig.session.state == SPEAKING
    rig.host(m.AudioFrame(1, 0, m.PLAYBACK, b"\x02\x00" * 100).to_bytes())
    rig.host(m.AudioFrame(9, 0, m.PLAYBACK, b"stale").to_bytes())  # another stream: dropped
    rig.host(m.AudioFrame(1, 1, m.MIC, b"wrong kind").to_bytes())
    assert rig.helper.played == [b"\x02\x00" * 100]
    rig.host(m.SpeakEnd(1))
    rig.session.handle("helper", {"event": "idle", "ms": 4200})
    assert rig.messages() == [m.PlaybackStopped(1, 4200, "done")] and rig.session.state == FOLLOW_UP


def test_talking_over_falak_stops_the_speakers_at_once_and_tells_the_host_where():
    rig = Rig()
    run_a_turn_until_speaking(rig)
    rig.host(m.AudioFrame(1, 0, m.PLAYBACK, b"\x02\x00" * 100).to_bytes())
    rig.session.handle("helper", {"event": "played", "ms": 1200})
    rig.frames(12, loud=True)  # 360 ms of him talking, past the warm-up
    assert {"cmd": "stop"} in rig.helper.commands  # stopped here: no message to the host was needed first
    kinds = rig.types()
    assert kinds[0] == "PlaybackStopped" and kinds[1] == "VoiceStart"
    assert rig.messages()[0] == m.PlaybackStopped(1, 1200, "barge_in")
    assert rig.session.state == LISTENING


def test_the_push_to_talk_key_works_with_the_wake_word_off():
    rig = Rig(wake=False)
    rig.frames(30, loud=True)
    assert rig.sent == []
    rig.session.handle("helper", {"event": "hotkey", "action": "down"})
    assert rig.session.state == LISTENING and rig.messages()[0] == m.VoiceStart(1, "push")
    rig.frames(5, loud=True)
    rig.now += 0.8
    rig.session.handle("helper", {"event": "hotkey", "action": "up"})  # held 0.8 s: hold to talk
    assert rig.types()[-1] == "VoiceEnd" and rig.session.state == WAITING


def test_chimes_can_be_turned_off_and_a_follow_up_window_ends_with_one():
    rig = Rig(chimes=False)
    say_hey_falak(rig)
    assert rig.helper.commands == []
    rig = Rig()
    rig.session.machine.state = FOLLOW_UP
    rig.session.machine._since = rig.now
    rig.now += 16
    rig.session.handle("tick")
    assert rig.session.state == IDLE and rig.helper.commands == [{"cmd": "chime", "name": "end"}]


def test_presence_is_reported_every_few_seconds():
    rig = Rig()
    rig.session.handle("tick")
    assert rig.messages() == [m.Presence(2.5, False)]
    rig.session.handle("tick")
    assert len(rig.sent) == 1  # not again so soon
    rig.now += 6
    rig.session.handle("tick")
    assert len(rig.sent) == 2


def test_a_helper_error_is_passed_on_and_closing_closes_the_helper():
    seen = []
    rig = Rig()
    rig.session.on_error = seen.append
    rig.session.handle("helper", {"event": "error", "what": "microphone not allowed"})
    assert seen == ["microphone not allowed"]
    rig.session.close()
    assert rig.helper.closed
