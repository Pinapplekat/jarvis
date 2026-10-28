"""What the engines take and give. Audio is PCM16 mono; a Voice makes it at one fixed rate."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Protocol

SAMPLE_RATE = 24000  # what every Voice returns


@dataclass(frozen=True)
class VoiceProfile:
    """How Falak sounds. Kokoro uses `voice` (one name, or two joined by a comma, which blends them equally) and
    `speed`; OmniVoice designs a voice from the attributes below; `say` uses `voice` as a macOS voice name."""

    voice: str = "bm_george"
    speed: float = 1.0
    gender: str | None = None  # OmniVoice: "male" or "female"
    age: str | None = None  # "child", "teenager", "young adult", "middle-aged" or "elderly"
    pitch: str | None = None  # "very low pitch" ... "very high pitch"
    accent: str | None = None  # an English accent, "british accent"
    whisper: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "VoiceProfile":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def instruct(self) -> str:
        """OmniVoice's voice-design string, e.g. "male, low pitch, british accent"."""
        parts = [self.gender, self.age, self.pitch, self.accent, "whisper" if self.whisper else None]
        return ", ".join(p for p in parts if p)


class Listener(Protocol):
    def transcribe(self, pcm16: bytes, rate: int) -> str: ...


class Voice(Protocol):
    def synthesize(self, sentence: str, profile: VoiceProfile) -> bytes: ...  # PCM16 mono at SAMPLE_RATE

    def unload(self) -> None: ...
