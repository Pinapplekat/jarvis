"""Speech, on-device: Whisper transcription (faster-whisper), Daniel-voice audio for
Telegram voice notes and phone calls (macOS `say` + ffmpeg), and splitting a streaming reply into
sentences so each can be spoken as soon as it's written. No audio leaves the Mac for this.

It also keeps the "speaking a live reply" flag, so helper reports wait instead of talking over
an answer at the desk. The desk mic, the Telegram listener and the tick are separate processes,
so the flag is a small file with an expiry: a crash can never leave it stuck on."""

import json
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

from jarvis import config
from jarvis.voice import speakable

SAMPLE_RATE = 16000
LIVE = config.DATA_DIR / "speaking.json"
LIVE_MAX_SECONDS = 180  # a live reply never holds reports back longer than this
CONVO = config.DATA_DIR / "conversation.json"  # the desk mic is mid-conversation (woken, follow-up open)
CONVO_MAX_SECONDS = 60  # refreshed each turn; a crash can't leave it stuck
_model = None
_model_lock = threading.Lock()


def live_reply_started(max_seconds: float = LIVE_MAX_SECONDS) -> None:
    """A live reply has started (set by the desk mic's answer)."""
    _write_live({"pid": os.getpid(), "until": time.time() + max_seconds})


def live_reply_ended() -> None:
    """The live reply ended or was barged in on. Safe to call twice."""
    try:
        LIVE.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        _write_live({"until": 0})  # can't remove it: at least make it stale


def conversation_started(max_seconds: float = CONVO_MAX_SECONDS) -> None:
    """He woke JARVIS at the desk (or just finished a turn and may follow up): announcements hold."""
    _write_live({"pid": os.getpid(), "until": time.time() + max_seconds}, CONVO)


def conversation_ended() -> None:
    try:
        CONVO.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        _write_live({"until": 0}, CONVO)


def in_conversation(now: float | None = None) -> bool:
    return _fresh(CONVO, now)


def busy(now: float | None = None) -> bool:
    """Is JARVIS talking with him at the desk right now (a live reply, or a conversation still open)?
    Anything else that wants to speak waits until this is False."""
    return is_speaking(now) or in_conversation(now)


def is_speaking(now: float | None = None) -> bool:
    """Is JARVIS speaking a live reply right now (in any process)?"""
    return _fresh(LIVE, now)


def _fresh(path: Path, now: float | None = None) -> bool:
    try:
        until = float(json.loads(path.read_text()).get("until") or 0)
    except (OSError, ValueError, AttributeError):
        return False
    return (now or time.time()) < until


def _write_live(data: dict, path: Path | None = None) -> None:
    path = path or LIVE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".speaking-")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError:
        pass  # never let the flag break speaking; reports just won't wait


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
    segments, _ = _whisper().transcribe(samples, vad_filter=True, beam_size=1, language="en",
                                        initial_prompt=VOCABULARY)
    return fix_names(" ".join(s.text.strip() for s in segments).strip())


# Names Whisper doesn't know. The prompt nudges it toward them; fix_names catches the usual slips
# where they're unambiguous (only at the start for the wake name: "Jeff, what's due?").
VOCABULARY = "Jarvis, Jev, Claude, Claude Code, Canvas, InQuizitive, Emily, Spotify, Telegram, Econ."
_NAME_FIXES = [
    (re.compile(r"^(?:(?P<lead>(?:hey|ok|okay)[,\s]+)(?:jeff|travis|drivers|jervis|jarvi|jarvas|service)\b"
                r"|(?:jeff|travis|drivers|jervis|jarvi|jarvas)(?=,))", re.I),
     lambda m: (m["lead"] or "") + "Jarvis"),
    (re.compile(r"\bclod(?=\s+code\b|\b)", re.I), lambda m: "Claude"),
    (re.compile(r"\bin\s?quisitive\b|\binquisitive\b(?=.*\b(?:question|card|assignment|chapter)s?\b)", re.I),
     lambda m: "InQuizitive"),
]


def fix_names(text: str) -> str:
    for pattern, repl in _NAME_FIXES:
        text = pattern.sub(repl, text)
    return text


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


def phone_audio(text: str) -> bytes:
    """Text -> 8 kHz mono WAV in JARVIS's voice, what a phone line carries (Twilio's <Play>)."""
    with tempfile.TemporaryDirectory() as tmp:
        aiff, wav = Path(tmp) / "say.aiff", Path(tmp) / "call.wav"
        subprocess.run(["say", "-v", config.VOICE_NAME, "-r", str(config.VOICE_RATE), "-o", str(aiff),
                        speakable(text)], check=True, timeout=60)
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(aiff), "-ar", "8000", "-ac", "1",
                        "-c:a", "pcm_s16le", str(wav)], check=True, timeout=60)
        return wav.read_bytes()


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
