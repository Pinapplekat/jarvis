"""The desk's conversation as a state machine. It knows nothing about audio: it is told what happened (the wake word,
the push-to-talk key, how long Elijah has been talking or quiet, what the host is sending) and answers with a list of
things to do. Times are seconds on any monotonic clock.

    idle      only the wake word and the push-to-talk key are listened for
    listening Elijah is talking; his voice is going to the host
    waiting   the host is thinking
    speaking  Falak's voice is playing
    follow-up listening without the wake word, for a while, after Falak finished or Elijah stopped talking
"""

from __future__ import annotations

from dataclasses import dataclass

IDLE, LISTENING, WAITING, SPEAKING, FOLLOW_UP = "idle", "listening", "waiting", "speaking", "follow-up"

# What to do, in order. Each is a tuple whose first item says what:
#   ("chime", "start" | "end")            a soft sound
#   ("voice_start", stream, source)       tell the host an utterance began
#   ("stream_mic", on)                    start or stop sending his voice (starting also sends the last half second)
#   ("voice_end", stream)                 tell the host the utterance ended
#   ("stop_playback", reason)             stop the speakers now
#   ("playback_stopped", stream, reason)  tell the host where playback stopped ("barge_in", "done" or "stop")
Effect = tuple


@dataclass(frozen=True)
class Timing:
    end_silence_ms: int = 700  # quiet this long ends his turn
    barge_in_ms: int = 250  # speech this long while Falak is speaking or thinking cuts it off
    echo_warmup_ms: int = 300  # Falak's first syllable, heard by the mic, must not count as him talking
    follow_up_s: float = 15.0
    follow_up_start_ms: int = 200  # speech this long opens a follow-up turn
    start_timeout_s: float = 6.0  # nothing said after the wake word: back to idle
    max_utterance_s: float = 30.0
    waiting_timeout_s: float = 45.0  # the host never answered
    tap_ms: int = 400  # a push-to-talk press shorter than this starts a conversation; longer is hold to talk


class DeskMachine:
    def __init__(self, timing: Timing = Timing()) -> None:
        self.timing = timing
        self.state = IDLE
        self.stream = 0  # the id of his current (or latest) utterance
        self.reply_stream = 0  # the stream of the audio being played or awaited
        self._since = 0.0  # when the current state began
        self._heard = False  # listening: he has said something
        self._push_at: float | None = None  # push-to-talk key is down since
        self._audio_ended = False  # speaking: the host sent SpeakEnd
        self._idle = True  # speaking: the speakers have played everything sent so far
        self.voice_barge_in = True  # off when macOS gave no echo cancellation: the mic then hears Falak's own voice

    # ---- what happened ----------------------------------------------------------------------------------------

    def wake(self, now: float) -> list[Effect]:
        """The wake word was heard (only listened for while idle or in a follow-up window)."""
        return self._listen(now, "wake", chime=True) if self.state in (IDLE, FOLLOW_UP) else []

    def push_down(self, now: float) -> list[Effect]:
        """The push-to-talk key went down: works from any state, and cuts Falak off if it is speaking."""
        if self.state == LISTENING:
            self._push_at = now
            return []
        out: list[Effect] = []
        if self.state in (SPEAKING, WAITING):
            out += self._cut(now, "barge_in")
        out += self._listen(now, "push", chime=True)
        self._push_at = now
        return out

    def push_up(self, now: float) -> list[Effect]:
        """Released after a hold: that was hold-to-talk, so the turn ends now. Released quickly: a tap, and the
        conversation goes on until he stops talking."""
        held, self._push_at = self._push_at, None
        if self.state == LISTENING and held is not None and (now - held) * 1000 >= self.timing.tap_ms:
            return self._end_turn(now)
        return []

    def vad(self, now: float, speech_ms: int, silence_ms: int) -> list[Effect]:
        """Called with every microphone frame: how long he has been talking without a real pause (0 if not talking)
        and how long it has been quiet since he last was."""
        t = self.timing
        if self.state == LISTENING:
            self._heard = self._heard or speech_ms > 0
            held = self._push_at is not None
            if self._heard and silence_ms >= t.end_silence_ms and not held:
                return self._end_turn(now)
            if now - self._since >= t.max_utterance_s:
                return self._end_turn(now)
            if not self._heard and not held and now - self._since >= t.start_timeout_s:
                return self._give_up(now)
        elif self.state == FOLLOW_UP and speech_ms >= t.follow_up_start_ms:
            return self._listen(now, "follow_up", chime=False)
        elif self.state == WAITING and speech_ms >= t.barge_in_ms:
            return self._barge_in(now)
        elif (self.state == SPEAKING and self.voice_barge_in and speech_ms >= t.barge_in_ms
              and (now - self._since) * 1000 >= t.echo_warmup_ms):
            return self._barge_in(now)
        return []

    def tick(self, now: float) -> list[Effect]:
        if self.state == FOLLOW_UP and now - self._since >= self.timing.follow_up_s:
            self.state, self._since = IDLE, now
            return [("chime", "end")]
        if self.state == WAITING and now - self._since >= self.timing.waiting_timeout_s:
            return self._follow_up(now)
        return []

    def host_speak_start(self, now: float, stream: int) -> list[Effect]:
        """The host's reply audio is about to arrive. Not while he is talking: a reply to something he has since
        talked over is stale."""
        if self.state == LISTENING:
            return []
        self.state, self._since, self.reply_stream = SPEAKING, now, stream
        self._audio_ended, self._idle = False, False
        return []

    def host_speak_end(self, now: float, stream: int) -> list[Effect]:
        """The host has nothing more to send for this stream."""
        if self.state == WAITING and stream == self.stream:
            return self._follow_up(now)  # it answered with nothing to say
        if self.state != SPEAKING or stream != self.reply_stream:
            return []
        self._audio_ended = True
        return self._maybe_done(now)

    def host_stop(self, now: float, stream: int) -> list[Effect]:
        if self.state == SPEAKING and stream == self.reply_stream:
            return self._cut(now, "stop") + self._follow_up(now)
        return []

    def playback_idle(self, now: float) -> list[Effect]:
        """The speakers have played everything they were given (there may be more coming)."""
        self._idle = True
        return self._maybe_done(now) if self.state == SPEAKING else []

    def playback_busy(self) -> None:
        self._idle = False

    # ---- inside -----------------------------------------------------------------------------------------------

    def _listen(self, now: float, source: str, chime: bool) -> list[Effect]:
        self.state, self._since, self._heard = LISTENING, now, False
        self.stream += 1
        out: list[Effect] = [("chime", "start")] if chime else []
        return out + [("voice_start", self.stream, source), ("stream_mic", True)]

    def _end_turn(self, now: float) -> list[Effect]:
        self.state, self._since, self._push_at = WAITING, now, None
        self.reply_stream = self.stream
        return [("stream_mic", False), ("voice_end", self.stream)]

    def _give_up(self, now: float) -> list[Effect]:
        """He never said anything after the wake word: tell the host the (empty) utterance is over."""
        self.state, self._since = IDLE, now
        return [("stream_mic", False), ("voice_end", self.stream), ("chime", "end")]

    def _follow_up(self, now: float) -> list[Effect]:
        self.state, self._since = FOLLOW_UP, now
        return []

    def _cut(self, now: float, reason: str) -> list[Effect]:
        out: list[Effect] = [("stop_playback", reason)] if self.state == SPEAKING else []
        return out + [("playback_stopped", self.reply_stream, reason)]

    def _barge_in(self, now: float) -> list[Effect]:
        return self._cut(now, "barge_in") + self._listen(now, "follow_up", chime=False)

    def _maybe_done(self, now: float) -> list[Effect]:
        if self._audio_ended and self._idle:
            out = [("playback_stopped", self.reply_stream, "done")]
            return out + self._follow_up(now)
        return []
