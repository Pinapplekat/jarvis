"""`jarvis voice setup` and `jarvis voice status`: put the host's speech models in Falak's own folder."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from jarvis.core.settings import Settings

MIN_FREE = 2 * 1024**3  # never take the disk below 2 GiB
KOKORO_FILES = ["*.json", "kokoro-v1_0.safetensors", "voices/*.safetensors"]  # ~330 MB; the repo's .pt voices are skipped


def _dirs(settings: Settings) -> list[Path]:
    from jarvis.channels.desk import LEGACY_MODELS

    return [settings.data_dir / "models", LEGACY_MODELS]


def _status_lines(settings: Settings) -> list[tuple[str, Path | None]]:
    from jarvis.speech.listener import WHISPER_REPO
    from jarvis.speech.models import find
    from jarvis.speech.voice import KOKORO_FOLDER, KOKORO_REPO

    dirs = _dirs(settings)
    return [("listening (Whisper base.en)", find(dirs, WHISPER_REPO.format(size="base.en"))),
            ("voice (Kokoro, MLX)", find(dirs, KOKORO_REPO, KOKORO_FOLDER))]


def status(settings: Settings) -> int:
    from jarvis.speech.voice import HOMEBREW_ESPEAK

    for name, path in _status_lines(settings):
        print(f"{name}: {path or 'missing (run `jarvis voice setup`)'}")
    if not HOMEBREW_ESPEAK.exists():
        print("pronunciation of unknown words: espeak-ng is not installed, so names and rare words (Emily, Spotify, "
              "Falak) are skipped when spoken. Fix: brew install espeak-ng")
    return 0


def setup(settings: Settings) -> int:
    """Copy the old JARVIS's Whisper model (read only) and download Kokoro for MLX, into Falak's models folder."""
    from jarvis.channels.desk import LEGACY_MODELS
    from jarvis.speech.listener import WHISPER_REPO
    from jarvis.speech.models import download, find, hf_snapshot
    from jarvis.speech.voice import KOKORO_FOLDER, KOKORO_REPO

    models = settings.data_dir / "models"
    models.mkdir(parents=True, exist_ok=True)
    repo = WHISPER_REPO.format(size="base.en")
    if find([models], repo) is None:
        old = hf_snapshot(LEGACY_MODELS, repo)
        if old is None:
            print("listening model: not found in the old JARVIS folder; it will be downloaded on first use")
        else:
            target = models / f"models--{repo.replace('/', '--')}" / "snapshots" / "copied"
            shutil.copytree(old, target, symlinks=False)  # follows the cache's links, so these are real files
            print(f"listening model: copied from the old JARVIS folder (read only) to {target}")
    if find([models], KOKORO_REPO, KOKORO_FOLDER) is None:
        free = shutil.disk_usage(models).free
        if free - 400 * 1024**2 < MIN_FREE:
            print(f"not downloading Kokoro: only {free / 1024**3:.1f} GiB free, and it would leave under 2 GiB",
                  file=sys.stderr)
            return 1
        print("downloading the Kokoro voice (about 330 MB)...")
        download(KOKORO_REPO, models / KOKORO_FOLDER, KOKORO_FILES)
    vad = models / "silero_vad.onnx"
    if not vad.exists():
        import urllib.request

        from jarvis.speech.vad import SILERO_URL

        print("downloading the speech detector for calls (2 MB)...")
        urllib.request.urlretrieve(SILERO_URL, vad)
    return status(settings)
