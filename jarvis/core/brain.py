"""The brain interface. The loop talks to this; only brain_sdk.py knows about Claude."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Image:
    mime: str
    data: str  # base64


@dataclass(frozen=True)
class ToolReply:
    text: str
    images: tuple[Image, ...] = ()
    is_error: bool = False


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    call: Callable[[dict], Awaitable[ToolReply]]


@dataclass
class BrainResult:
    text: str
    tool_calls: list[str] = field(default_factory=list)  # names, in order
    num_turns: int = 0
    cost_usd: float | None = None
    error: str | None = None


TextSink = Callable[[str], None]  # gets each piece of text as the model writes it


class Brain(Protocol):
    async def run(self, system: str, prompt: str, tools: list[Tool], on_text: TextSink | None = None) -> BrainResult:
        """`on_text` is only passed by voice channels, which say the text while the rest is still being written."""
        ...
