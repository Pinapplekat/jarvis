"""Text to speech: one engine loaded at a time, kept warm, dropped after it has been idle."""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from jarvis.speech.types import SAMPLE_RATE, Voice, VoiceProfile

log = logging.getLogger("jarvis.speech.voice")

ENGINES = ("kokoro", "omnivoice", "say")
KOKORO_FOLDER = "kokoro-mlx"  # mlx-audio picks the model class from the folder name
KOKORO_REPO = "mlx-community/Kokoro-82M-bf16"
OMNIVOICE_REPO = "k2-fsa/OmniVoice"
HOMEBREW_ESPEAK = Path("/opt/homebrew/lib/libespeak-ng.dylib")


def to_pcm16(samples) -> bytes:
    import numpy as np

    return (np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0) * 32767).astype("<i2").tobytes()


class KokoroVoice:
    """Kokoro through MLX on the GPU. Slower to warm up (about 5 s, once) than torch on the CPU, but about 20%
    faster per sentence and it needs no torch."""

    def __init__(self, model_dir: Path) -> None:
        self._dir, self._model = model_dir, None

    def _load(self):
        if self._model is None:
            from misaki import espeak
            from mlx_audio.tts.utils import load

            if HOMEBREW_ESPEAK.exists():
                from phonemizer.backend.espeak.wrapper import EspeakWrapper

                EspeakWrapper.set_library(str(HOMEBREW_ESPEAK))  # misaki points at a wheel that is broken on macOS
            else:
                # The espeakng-loader wheel kills the process on macOS. Without espeak, Kokoro skips every word that
                # is missing from its dictionary, and that includes names (Emily, Spotify, Falak): install it with
                # `brew install espeak-ng` (`jarvis voice status` says when it is missing).
                def unavailable(*_a, **_k):
                    raise RuntimeError("espeak is not installed")

                espeak.EspeakFallback = unavailable
            self._model = load(str(self._dir))
        return self._model

    def synthesize(self, sentence: str, profile: VoiceProfile) -> bytes:
        import numpy as np

        model = self._load()
        lang = "b" if profile.voice.split(",")[0].startswith("b") else "a"
        parts = [np.array(r.audio) for r in model.generate(sentence, voice=profile.voice, speed=profile.speed,
                                                           lang_code=lang)]
        return to_pcm16(np.concatenate(parts)) if parts else b""

    def unload(self) -> None:
        self._model = None


class OmniVoiceVoice:
    """OmniVoice designing a voice from attributes. Too slow for this Mac (11 s a sentence); kept for the Mac mini."""

    def __init__(self, model_dir: Path, steps: int = 16) -> None:
        self._dir, self._steps, self._model = model_dir, steps, None

    def _load(self):
        if self._model is None:
            import torch
            from omnivoice import OmniVoice

            # Loading straight onto MPS crashes in transformers' weight loader, so load on the CPU and move it.
            self._model = OmniVoice.from_pretrained(str(self._dir), device_map="cpu", dtype=torch.float16).to("mps")
        return self._model

    def synthesize(self, sentence: str, profile: VoiceProfile) -> bytes:
        model = self._load()
        audio = model.generate(text=sentence, instruct=profile.instruct() or None, num_step=self._steps)
        return to_pcm16(audio[0]) if int(model.sampling_rate) == SAMPLE_RATE else _resample(audio[0], model.sampling_rate)

    def unload(self) -> None:
        self._model = None


def _resample(samples, rate: int) -> bytes:
    import numpy as np

    x = np.asarray(samples, dtype=np.float32)
    n = int(x.size * SAMPLE_RATE / rate)
    return to_pcm16(np.interp(np.linspace(0, x.size - 1, n), np.arange(x.size), x))


class SayVoice:
    """macOS `say`, the last resort. It can only write to a file, so each sentence passes through a private temp
    file that is deleted before the call returns."""

    def __init__(self, name: str = "Daniel") -> None:
        self._name = name

    def synthesize(self, sentence: str, profile: VoiceProfile) -> bytes:
        fd, path = tempfile.mkstemp(suffix=".wav")  # 0600
        os.close(fd)
        try:
            rate = int(175 * profile.speed)
            subprocess.run(["say", "-v", self._name, "-r", str(rate), "-o", path, "--file-format=WAVE",
                            f"--data-format=LEI16@{SAMPLE_RATE}", sentence], check=True, capture_output=True, timeout=30)
            return Path(path).read_bytes()[44:]
        finally:
            Path(path).unlink(missing_ok=True)

    def unload(self) -> None:
        pass


class VoiceManager:
    """The one loaded Voice. `synthesize` loads the engine on first use, `warm` loads it ahead of a conversation, and
    `tick` (called every so often by the host) unloads it after `idle_minutes` without use."""

    def __init__(self, make: Callable[[str], Voice], idle_minutes: float = 30,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._make, self._idle, self._clock = make, idle_minutes * 60, clock
        self._engine = ""
        self._voice: Voice | None = None
        self._used = 0.0
        self._lock = threading.Lock()

    def _get(self, engine: str) -> Voice:
        if self._voice is None or engine != self._engine:
            if self._voice is not None:
                self._voice.unload()  # one engine at a time: the old one goes first
            self._voice, self._engine = self._make(engine), engine
        return self._voice

    def synthesize(self, engine: str, sentence: str, profile: VoiceProfile) -> bytes:
        with self._lock:
            voice = self._get(engine)
            self._used = self._clock()
            return voice.synthesize(sentence, profile)

    def warm(self, engine: str, profile: VoiceProfile) -> None:
        """Load the engine and run a short sentence so the first real one is fast (MLX compiles on first use)."""
        self.synthesize(engine, "Ready.", profile)

    def tick(self) -> bool:
        """Unload when idle for long enough. Returns whether it did."""
        with self._lock:
            if self._voice is not None and self._clock() - self._used >= self._idle:
                self._voice.unload()
                self._voice, self._engine = None, ""
                log.info("voice engine unloaded after %.0f minutes idle", self._idle / 60)
                return True
        return False

    @property
    def loaded(self) -> str:
        return self._engine if self._voice is not None else ""
