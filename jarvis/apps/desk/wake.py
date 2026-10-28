"""The wake word and speech detection, on onnx models in the node's own folder. numpy and openwakeword come from the
`desk` extra and are imported only when the desk starts."""

from __future__ import annotations

import logging
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("jarvis.apps.desk")

CUSTOM = "hey_falak.onnx"  # trained from docs/wake-word.md; the pretrained hey_jarvis stands in until it exists
FALLBACK = "hey_jarvis_v0.1.onnx"
THRESHOLD = 0.5
COOLDOWN_S = 2.0
MODELS = "models"


def models_dir(desk_dir: Path) -> Path:
    return desk_dir / MODELS


def wake_model_path(desk_dir: Path) -> Path | None:
    for path in (desk_dir / CUSTOM, models_dir(desk_dir) / FALLBACK):
        if path.exists():
            return path
    return None


def download_models(desk_dir: Path) -> None:
    """Fetch the feature models, the Silero VAD and the pretrained stand-in into the node's folder (once)."""
    from openwakeword.utils import download_models as fetch

    fetch(model_names=["hey_jarvis"], target_directory=str(models_dir(desk_dir)))


def load_vad(desk_dir: Path) -> Callable[[bytes], float]:
    """Silero VAD: speech probability of a 30 ms frame of 16 kHz PCM16."""
    import numpy as np
    from openwakeword.vad import VAD

    vad = VAD(model_path=str(models_dir(desk_dir) / "silero_vad.onnx"))
    return lambda pcm: float(vad.predict(np.frombuffer(pcm, dtype=np.int16), frame_size=480))


def load_wake(desk_dir: Path, clock: Callable[[], float]) -> Callable[[bytes], bool] | None:
    """openWakeWord for 'Hey Falak' (or the stand-in); None when no model is there, and the push-to-talk key still works."""
    import numpy as np
    from openwakeword.model import Model

    path = wake_model_path(desk_dir)
    if path is None:
        return None
    models = models_dir(desk_dir)
    model = Model(wakeword_models=[str(path)], inference_framework="onnx",
                  melspec_model_path=str(models / "melspectrogram.onnx"),
                  embedding_model_path=str(models / "embedding_model.onnx"))
    log.info("desk: listening for the wake word with %s", path.name)
    last = [-COOLDOWN_S]

    def heard(chunk: bytes) -> bool:
        scores = model.predict(np.frombuffer(chunk, dtype=np.int16))
        if max(scores.values(), default=0.0) >= THRESHOLD and clock() - last[0] >= COOLDOWN_S:
            last[0] = clock()
            model.reset()
            return True
        return False

    return heard


def idle_seconds() -> float | None:
    """Seconds since the last keyboard or mouse input, or None if it can't be read."""
    try:
        out = subprocess.run(["ioreg", "-c", "IOHIDSystem", "-d", "4"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    found = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
    return int(found[1]) / 1e9 if found else None
