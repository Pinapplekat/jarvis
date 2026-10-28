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
        self._loop: asyncio.AbstractEventLoop | None = None
        self._kick: asyncio.Event | None = None
        runtime.on_event = self._poke
        runtime.send_frame = self._send_frame
        self._frames: asyncio.Queue | None = None  # voice frames for the host, in order, from any thread
        self.connected = threading.Event()  # set while welcomed; tests and --with-node wait on it

    def _note(self, state: str, message: str, *args: object) -> None:
        """Log a state change once, not on every retry."""
        if state != self._state:
            self._state = state
            log.info(message, *args)

    def _poke(self) -> None:
        """An app queued an event (any thread): wake the sender of the current connection, if there is one."""
        if self._loop is not None and self._kick is not None:
            self._loop.call_soon_threadsafe(self._kick.set)

    def _send_frame(self, frame: str | bytes) -> None:
        """A voice frame from an app thread. Live audio is never queued for later: dropped when not connected."""
        if self._loop is not None and self._frames is not None and self.connected.is_set():
            self._loop.call_soon_threadsafe(self._frames.put_nowait, frame)

    async def run(self) -> None:
        self._stop = asyncio.Event()
        self._loop, self._kick = asyncio.get_running_loop(), asyncio.Event()
        self._frames = asyncio.Queue()
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
            pump = asyncio.create_task(self._pump(ws))
            voice = asyncio.create_task(self._voice_pump(ws))
            try:
                async for frame in ws:
                    if m.is_audio(frame):
                        self._runtime.host_frame(frame)
                        continue
                    msg = m.parse(frame)
                    if isinstance(msg, m.EventAck):
                        self._runtime.ack(msg.id)
                    elif isinstance(msg, m.Call):
                        task = asyncio.create_task(self._answer(ws, msg))
                        tasks.add(task)
                        task.add_done_callback(tasks.discard)
                    elif isinstance(msg, m.Ping):
                        await ws.send(m.Pong(msg.t).to_json())
                    elif isinstance(msg, (m.SpeakStart, m.SpeakEnd, m.SpeakStop)):
                        self._runtime.host_frame(frame)
                    elif msg is not None and not isinstance(msg, m.Pong):
                        log.warning("the host sent an unexpected %s; dropped", msg.type)
            except ConnectionClosed:
                pass
            finally:
                pump.cancel()
                voice.cancel()
                while not self._frames.empty():  # audio from before the drop is stale
                    self._frames.get_nowait()
                for task in tasks:
                    task.cancel()
                self._ws = None
                closed = await asyncio.get_running_loop().run_in_executor(None, self._runtime.close_live)
                if closed:
                    log.info("closed %d live view(s) because the host link dropped", closed)
            return True

    async def _pump(self, ws: ClientConnection) -> None:
        """Send every unacked event, oldest first: all of them when the link comes up, then new ones as apps queue
        them. An event sent but not yet acked is not sent again on this connection."""
        sent: set[str] = set()
        try:
            while True:
                self._kick.clear()
                for event in await asyncio.get_running_loop().run_in_executor(None, self._runtime.unacked):
                    if event.id not in sent:
                        sent.add(event.id)
                        await ws.send(event.to_json())
                await self._kick.wait()
        except ConnectionClosed:
            return

    async def _voice_pump(self, ws: ClientConnection) -> None:
        try:
            while True:
                await ws.send(await self._frames.get())
        except ConnectionClosed:
            return

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
