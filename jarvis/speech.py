"""Speech, on-device: Whisper transcription (faster-whisper), Daniel-voice audio for
Telegram voice notes (macOS `say` + ffmpeg), and splitting a streaming reply into
sentences so each can be spoken as soon as it's written. No audio leaves the Mac for this."""

import re
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


# Words whose trailing period doesn't end a sentence ("Dr. G", "e.g. this", "5 p.m. today").
ABBREVIATIONS = {"dr", "mr", "mrs", "ms", "prof", "st", "jr", "sr", "vs", "etc", "e.g", "i.e", "a.m",
                 "p.m", "no", "approx", "dept", "est", "fig", "inc", "ltd", "mt", "ave", "jan", "feb",
                 "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec", "mon", "tue",
                 "wed", "thu", "fri", "sat", "sun", "u.s"}
_BOUNDARY = re.compile(r"[.!?]+[\"')\]]*(?=\s)|\n")
_WORD_BEFORE = re.compile(r"([A-Za-z][A-Za-z.]*)\.$")


class SentenceBuffer:
    """Feed it streaming text; get back each sentence once it's clearly finished (its end
    mark is followed by whitespace, or the line ended). `flush()` returns the tail.

    `text` is everything fed so far and `consumed` how much of it has been handed out."""

    def __init__(self):
        self.text, self.consumed = "", 0

    def feed(self, delta: str) -> list[str]:
        self.text += delta
        return self._take()

    def feed_text(self, so_far: str) -> list[str]:
        """Same, given the whole text so far (what the reply streamer reports)."""
        if not so_far.startswith(self.text):
            return []  # the stream rewrote itself; the final reply sorts it out
        return self.feed(so_far[len(self.text):])

    def flush(self) -> list[str]:
        tail = self.text[self.consumed:].strip()
        self.consumed = len(self.text)
        return [tail] if tail else []

    def _take(self) -> list[str]:
        out, start = [], self.consumed
        for m in _BOUNDARY.finditer(self.text, start):
            if m[0] != "\n" and _abbreviation(self.text[start:m.start() + 1]):
                continue
            sentence = self.text[start:m.end()].strip()
            if sentence:
                out.append(sentence)
            start = m.end()
        self.consumed = start
        return out


def _abbreviation(chunk: str) -> bool:
    """Does `chunk` end with a period that belongs to an abbreviation or an initial?"""
    if not chunk.endswith("."):
        return False
    m = _WORD_BEFORE.search(chunk)
    if not m:
        return False
    word = m[1].lower()
    return word in ABBREVIATIONS or (len(word) == 1 and m[1].isupper())


def sentences(text: str) -> list[str]:
    """A whole reply split the same way as a stream."""
    buf = SentenceBuffer()
    return buf.feed(text) + buf.flush()
