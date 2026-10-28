"""Speech to text. The host does this; the desk and the call only send audio."""

from __future__ import annotations

import threading
from pathlib import Path

WHISPER_REPO = "Systran/faster-whisper-{size}"
VOCABULARY = "Falak, Claude, Discord, Spotify, Emily, Canvas, sir."  # nudges names Whisper would otherwise mishear


def to_16k_float(pcm16: bytes, rate: int):
    import numpy as np

    audio = np.frombuffer(pcm16[: len(pcm16) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768.0
    if rate != 16000 and audio.size:
        n = int(audio.size * 16000 / rate)
        audio = np.interp(np.linspace(0, audio.size - 1, n), np.arange(audio.size), audio).astype(np.float32)
    return audio


class WhisperListener:
    """faster-whisper, int8 on the CPU (base.en: 0.5 s for a 4 s clip, about 600 MB; see docs/voice-measurements.md)."""

    def __init__(self, model_dir: Path) -> None:
        self._dir, self._model, self._lock = model_dir, None, threading.Lock()

    def _load(self):
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel

                self._model = WhisperModel(str(self._dir), device="cpu", compute_type="int8")
            return self._model

    def transcribe(self, pcm16: bytes, rate: int) -> str:
        audio = to_16k_float(pcm16, rate)
        if audio.size < 1600:  # under 0.1 s is a click, not speech
            return ""
        segments, _ = self._load().transcribe(audio, language="en", beam_size=1, initial_prompt=VOCABULARY,
                                              condition_on_previous_text=False)
        return " ".join(s.text.strip() for s in segments).strip()

    def unload(self) -> None:
        with self._lock:
            self._model = None
