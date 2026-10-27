"""A channel for tests: tests push messages in, and it records what the host sends back."""

import asyncio
from contextlib import asynccontextmanager


class FakeChannel:
    def __init__(self, name="discord"):
        self.name = name
        self.sent, self.events, self.stopped = [], [], False
        self.images = []  # what each send carried, alongside sent
        self._deliver = None
        self._done = asyncio.Event()
        self._ready = asyncio.Event()

    async def start(self, deliver):
        self._deliver = deliver
        self._ready.set()
        await self._done.wait()

    async def stop(self):
        self.stopped = True
        self._done.set()

    async def send(self, text, *, reply_to=None, ask_id=None, images=()):
        self.events.append(("send", text))
        self.sent.append((text, reply_to, ask_id))
        self.images.append(tuple(images))

    @asynccontextmanager
    async def typing(self, reply_to):
        self.events.append(("typing on", reply_to))
        try:
            yield
        finally:
            self.events.append(("typing off", reply_to))

    def finish(self):
        self._done.set()

    async def deliver(self, incoming):
        await self._ready.wait()
        await self._deliver(incoming)
