"""The node's connection to the host: say hello, run calls in worker threads, reconnect when it drops."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from jarvis.node.config import NodeConfig
from jarvis.node.runtime import NodeRuntime
from jarvis.protocol import messages as m

log = logging.getLogger("jarvis.node.client")

BACKOFF = (1, 2, 5, 10, 30)  # seconds between attempts; the last repeats
MAX_FRAME = 16 * 1024 * 1024
WORKERS = 8


class NodeClient:
    def __init__(self, config: NodeConfig, runtime: NodeRuntime, backoff: tuple[float, ...] = BACKOFF) -> None:
        self._config, self._runtime, self._backoff = config, runtime, backoff
        self._pool = ThreadPoolExecutor(WORKERS, thread_name_prefix="node-call")
        self._stop: asyncio.Event | None = None
        self._ws: ClientConnection | None = None
        self._state = ""
        self.connected = threading.Event()  # set while welcomed; tests and --with-node wait on it

    def _note(self, state: str, message: str, *args: object) -> None:
        """Log a state change once, not on every retry."""
        if state != self._state:
            self._state = state
            log.info(message, *args)

    async def run(self) -> None:
        self._stop = asyncio.Event()
        attempt = 0
        while not self._stop.is_set():
            try:
                welcomed = await self._session()
            except (OSError, WebSocketException, asyncio.TimeoutError) as e:
                welcomed = False
                self._note("down", "can't reach the host at %s (%s); retrying", self._config.host_url, type(e).__name__)
            self.connected.clear()
            if welcomed:
                attempt = 0
                self._note("down", "disconnected from the host; reconnecting")
            if self._stop.is_set():
                break
            wait = self._backoff[min(attempt, len(self._backoff) - 1)]
            attempt += 1
            try:
                await asyncio.wait_for(self._stop.wait(), wait)
            except asyncio.TimeoutError:
                pass
        self._pool.shutdown(wait=False, cancel_futures=True)

    def stop(self) -> None:
        """Ask run() to finish. Safe to call from a signal handler on the node's loop."""
        if self._stop is not None:
            self._stop.set()
        if self._ws is not None:
            asyncio.ensure_future(self._ws.close())

    async def _session(self) -> bool:
        """One connection. Returns whether the host welcomed us."""
        async with connect(self._config.host_url, ping_interval=None, max_size=MAX_FRAME) as ws:
            self._ws = ws
            await ws.send(m.Hello(self._config.name, self._config.key or "", self._runtime.capabilities,
                                  self._runtime.apps).to_json())
            reply = m.parse(await asyncio.wait_for(ws.recv(), 15))
            if isinstance(reply, m.Reject):
                self._note("rejected", "the host rejected this node: %s", reply.reason)
                return False
            if not isinstance(reply, m.Welcome):
                self._note("down", "the host did not welcome this node; retrying")
                return False
            self._runtime.configure(reply.settings)
            self._note("up", "connected to the host as %s (apps: %s)", self._config.name, ", ".join(sorted(reply.settings)))
            self.connected.set()
            tasks: set[asyncio.Task] = set()
            try:
                async for frame in ws:
                    msg = m.parse(frame)
                    if isinstance(msg, m.Call):
                        task = asyncio.create_task(self._answer(ws, msg))
                        tasks.add(task)
                        task.add_done_callback(tasks.discard)
                    elif isinstance(msg, m.Ping):
                        await ws.send(m.Pong(msg.t).to_json())
                    elif msg is not None and not isinstance(msg, m.Pong):
                        log.warning("the host sent an unexpected %s; dropped", msg.type)
            except ConnectionClosed:
                pass
            finally:
                for task in tasks:
                    task.cancel()
                self._ws = None
                closed = await asyncio.get_running_loop().run_in_executor(None, self._runtime.close_live)
                if closed:
                    log.info("closed %d live view(s) because the host link dropped", closed)
            return True

    async def _answer(self, ws: ClientConnection, call: m.Call) -> None:
        loop = asyncio.get_running_loop()
        abandoned = threading.Event()
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(loop.run_in_executor(self._pool, self._runtime.execute, call, abandoned),
                                            call.timeout_s)
        except asyncio.TimeoutError:
            abandoned.set()
            result = m.CallResult(call.id, False, error=f"{call.app} didn't finish within {int(call.timeout_s)} seconds.")
        except asyncio.CancelledError:
            abandoned.set()
            raise
        log.info("call %s %s.%s ok=%s ms=%d", call.id, call.app, call.handler, result.ok,
                 (time.monotonic() - started) * 1000)
        try:
            await ws.send(result.to_json())
        except ConnectionClosed:
            pass
