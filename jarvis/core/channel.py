"""The channel interface: every way of talking to Falak (Discord, the terminal, later the desk) plugs in here."""

from __future__ import annotations

import asyncio
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


class Speech(Protocol):
    """A voice channel's side of one turn (the desk, a call): the host feeds it the text as the model writes it, and
    the channel says it. Channels that speak also have `begin_speech(reply_to) -> Speech`, used instead of `send`."""

    cancelled: asyncio.Event  # set when Elijah talks over Falak before it has answered

    def on_text(self, delta: str) -> None: ...  # a piece of text; sentences are said as they complete

    def on_tool(self) -> None: ...  # an app action is starting: if nothing has been said yet, fill the silence

    def mute(self) -> None: ...  # stop saying anything: Elijah interrupted after a tool had already run

    async def finish(self, reply: str) -> str | None:
        """The turn is over; say whatever is left of `reply`. Returns what to write in the conversation log in place
        of `reply` (what was said, marked as interrupted), or None when all of it was said."""
        ...


class Channel(Protocol):
    """Adapters deliver only Elijah's messages; anything else is dropped before it reaches the core."""

    name: str

    async def start(self, deliver: Deliver) -> None: ...  # runs until stopped

    async def stop(self) -> None: ...

    async def send(self, text: str, *, reply_to: object = None, ask_id: int | None = None,
                   images: tuple[Picture, ...] = ()) -> None: ...  # images go with the last piece of text

    def typing(self, reply_to: object) -> AbstractAsyncContextManager[None]: ...  # shown while a turn runs
