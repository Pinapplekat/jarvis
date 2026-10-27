"""The terminal as a channel: `jarvis chat` reads lines, prints replies. Elijah is the only one who can type here."""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager

from jarvis.core.channel import Deliver, Incoming

LEAVE = ("bye", "exit", "quit")


class TerminalChannel:
    name = "cli"

    def __init__(self) -> None:
        self._replied = asyncio.Event()
        self._stopped = False

    async def _readline(self) -> str | None:
        """Read on our own daemon thread: a thread stuck in input() must not hold up shutdown."""
        loop = asyncio.get_running_loop()
        done: asyncio.Future[str | None] = loop.create_future()

        def read() -> None:
            try:
                line: str | None = input("> ")
            except (EOFError, KeyboardInterrupt):
                line = None
            try:
                loop.call_soon_threadsafe(lambda: done.done() or done.set_result(line))
            except RuntimeError:  # the loop closed while we waited
                pass

        threading.Thread(target=read, daemon=True).start()
        return await done

    async def start(self, deliver: Deliver) -> None:
        while not self._stopped:
            line = await self._readline()
            if line is None:
                print()
                return
            line = line.strip()
            if line.lower() in LEAVE:
                return
            if line:
                self._replied.clear()
                await deliver(Incoming("cli", line))
                await self._replied.wait()  # the next prompt waits for the reply

    async def stop(self) -> None:
        self._stopped = True

    async def send(self, text: str, *, reply_to: object = None, ask_id: int | None = None,
                   images: tuple = ()) -> None:
        print(text)  # yes/no is typed in the terminal, so ask_id needs nothing extra
        if images:
            print(f"({len(images)} image{'s' if len(images) > 1 else ''} not shown in the terminal)")
        self._replied.set()

    @asynccontextmanager
    async def typing(self, reply_to: object):
        yield
