"""Turns the speech probability of each 30 ms microphone frame into 'how long has he been talking' and 'how long has it
been quiet', which is all the state machine needs."""

from __future__ import annotations

FRAME_MS = 30


class SpeechTracker:
    def __init__(self, threshold: float = 0.5, hangover_ms: int = 150) -> None:
        self.threshold, self.hangover_ms = threshold, hangover_ms
        self.reset()

    def reset(self) -> None:
        self._speech_ms = 0
        self._quiet_ms = 0

    def feed(self, probability: float) -> tuple[int, int]:
        """(speech_ms, silence_ms) after this frame. A pause shorter than the hangover does not end a speech run, so
        a breath between words is still one stretch of talking; the silence counter counts every quiet frame."""
        if probability >= self.threshold:
            self._speech_ms += FRAME_MS
            self._quiet_ms = 0
        else:
            self._quiet_ms += FRAME_MS
            if self._quiet_ms >= self.hangover_ms:
                self._speech_ms = 0
        return self._speech_ms, self._quiet_ms
