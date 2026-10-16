"""Speech, on-device: Whisper transcription (faster-whisper) and Daniel-voice audio for
Telegram voice notes (macOS `say` + ffmpeg). No audio leaves the Mac for this."""

import subprocess
import tempfile
import threading
from pathlib import Path

import numpy as np

from jarvis import config
from jarvis.voice import speakable

SAMPLE_RATE = 16000
_model = None
_model_lock = threading.Lock()


def _whisper():
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel
            root = config.DATA_DIR / "models"
            root.mkdir(parents=True, exist_ok=True)
            _model = WhisperModel(config.WHISPER_MODEL, device="cpu", compute_type="int8",
                                  download_root=str(root))
        return _model


def decode(data: bytes) -> np.ndarray:
    """Any audio (Telegram's OGG/Opus, AIFF, WAV...) -> 16 kHz mono float32, via ffmpeg."""
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-f", "f32le", "-ac", "1",
                        "-ar", str(SAMPLE_RATE), "pipe:1"], input=data, capture_output=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"couldn't decode audio: {r.stderr.decode(errors='replace')[:200]}")
    return np.frombuffer(r.stdout, dtype=np.float32)


def transcribe(audio: np.ndarray | bytes) -> str:
    """Speech to text. Accepts raw 16 kHz float32 samples or encoded audio bytes."""
    if isinstance(audio, bytes) and not audio:
        return ""
    samples = decode(audio) if isinstance(audio, bytes) else audio
    if samples.size < SAMPLE_RATE * 0.3:
        return ""
    segments, _ = _whisper().transcribe(samples, vad_filter=True, beam_size=1, language="en")
    return " ".join(s.text.strip() for s in segments).strip()


def voice_note(text: str) -> bytes:
    """Text -> OGG/Opus voice note in JARVIS's voice (what Telegram's sendVoice wants)."""
    spoken = speakable(text)
    with tempfile.TemporaryDirectory() as tmp:
        aiff, ogg = Path(tmp) / "say.aiff", Path(tmp) / "note.ogg"
        subprocess.run(["say", "-v", config.VOICE_NAME, "-r", str(config.VOICE_RATE), "-o", str(aiff), spoken],
                       check=True, timeout=60)
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(aiff), "-c:a", "libopus",
                        "-b:a", "32k", str(ogg)], check=True, timeout=60)
        return ogg.read_bytes()
