"""The channel interface: every way of talking to Falak (Discord, the terminal, later the desk) plugs in here."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol

from jarvis.contract.api import Attachment


@dataclass(frozen=True)
class Incoming:
    channel: str  # "discord", "cli", ...
    text: str
    attachments: tuple[Attachment, ...] = ()  # files Elijah sent, saved in the core tmp folder
    reply_to: object = None  # adapter-specific handle (e.g. the Discord DM), opaque to the core
    untrusted: bool = False  # it quotes someone else's words (a forwarded message), so the turn is outside content


@dataclass(frozen=True)
class Picture:
    """An image going out with a reply. Held in memory only; never written to disk."""

    mime: str
    data: bytes


@dataclass(frozen=True)
class AskResult:
    ok: bool  # false when the ask couldn't be answered (already answered, expired, ...)
    message: str  # a short line to show Elijah


Deliver = Callable[[Incoming], Awaitable[None]]
AnswerAsk = Callable[[int, bool], Awaitable[AskResult]]  # the host's way to answer an ask from a button


class ChannelUnavailable(Exception):
    """A channel can't start (missing token, missing setting). The message is one plain line for Elijah."""


class Channel(Protocol):
    """Adapters deliver only Elijah's messages; anything else is dropped before it reaches the core."""

    name: str

    async def start(self, deliver: Deliver) -> None: ...  # runs until stopped

    async def stop(self) -> None: ...

    async def send(self, text: str, *, reply_to: object = None, ask_id: int | None = None,
                   images: tuple[Picture, ...] = ()) -> None: ...  # images go with the last piece of text

    def typing(self, reply_to: object) -> AbstractAsyncContextManager[None]: ...  # shown while a turn runs
