"""What the desk and the Discord call share: one listener, one loaded voice, the profile, and the way a trailing
'and...' is held. Both channels run the same pipeline: transcribe, turn, sentences, voice, barge-in."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from jarvis.speech import Listener, VoiceManager, VoiceProfile

log = logging.getLogger("jarvis.channels.voice")

# A turn that trails off ("and...", "um") is held for a moment: he usually carries on, and the two parts are one request.
TRAILING = re.compile(r"(?:\b(?:and|um+|uh+|er|so|but|or|then|because)\b|…|\.\.\.)[\s,.]*$", re.IGNORECASE)
GRACE_S = 2.5
MAX_GRACES = 2


@dataclass
class VoiceServices:
    listener: Callable[[], Listener]
    voice: VoiceManager
    engine: Callable[[], str]
    profile: Callable[[], VoiceProfile]
    _listener: Listener | None = field(default=None, init=False)

    def transcribe(self, pcm16: bytes, rate: int = 16000) -> str:
        if self._listener is None:
            self._listener = self.listener()
        return self._listener.transcribe(pcm16, rate)

    async def synthesize(self, sentence: str, profile: VoiceProfile) -> bytes:
        """Speak with the configured engine; if it can't (not downloaded, crashed), macOS `say` does, as the last resort."""
        engine = self.engine()
        try:
            return await asyncio.to_thread(self.voice.synthesize, engine, sentence, profile)
        except Exception:
            if engine == "say":
                raise
            log.exception("the %s voice failed; using macOS say for this sentence", engine)
            return await asyncio.to_thread(self.voice.synthesize, "say", sentence, VoiceProfile(voice="Daniel"))

    def warm_in_background(self) -> None:
        """A conversation is starting: load the voice now so the first sentence doesn't wait for the model."""
        if self.voice.loaded:
            return
        engine, profile = self.engine(), self.profile()

        def load() -> None:
            try:
                self.voice.warm(engine, profile)
            except Exception:
                log.exception("could not load the %s voice", engine)

        asyncio.ensure_future(asyncio.to_thread(load))


class Joiner:
    """Holds a request that trails off and joins it with the next one; releases it unchanged if nothing follows."""

    def __init__(self, deliver: Callable[[str, object], Awaitable[None]]) -> None:
        self._deliver = deliver
        self._held: tuple[str, object, int] | None = None  # text, reply_to, graces used
        self._timer: asyncio.TimerHandle | None = None

    async def offer(self, text: str, reply_to: object) -> None:
        text, graces = text.strip(), 0
        if self._held is not None:
            held_text, _, graces = self._held
            self.cancel()
            text = f"{held_text} {text}"
        if TRAILING.search(text) and graces < MAX_GRACES:
            self._held = (text, reply_to, graces + 1)
            self._timer = asyncio.get_running_loop().call_later(_grace(), lambda: asyncio.ensure_future(self._release()))
            return
        await self._deliver(text, reply_to)

    @property
    def holding(self) -> bool:
        return self._held is not None

    def cancel(self) -> None:
        self._held = None
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    async def _release(self) -> None:
        held, self._held, self._timer = self._held, None, None
        if held is not None:
            await self._deliver(held[0], held[1])


def _grace() -> float:
    return GRACE_S  # read when used, so tests can shorten it
