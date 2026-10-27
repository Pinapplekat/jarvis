"""The WebSocket server device nodes connect to, and the one place a call reaches a node.

It runs in its own thread with its own event loop, so a handler waiting on a camera blocks only the thread
that asked (an action worker), never the host's loop. Everything here that needs the database uses the hub's
own connection, opened in its thread.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import itertools
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from jarvis.core import db, outbox
from jarvis.core.dispatch import NodeError, NodeInfo, NodeReply, Target
from jarvis.core.nodes import hash_key
from jarvis.core.settings import Settings
from jarvis.core.timeutil import stamp
from jarvis.protocol import messages as m

log = logging.getLogger("jarvis.core.node_hub")

HELLO_WAIT = 10  # seconds a new connection has to say hello
ANSWER_GRACE = 5  # seconds on top of a call's own timeout; the node answers its own timeouts first
MAX_FRAME = 16 * 1024 * 1024  # 10 MB of pictures plus base64 overhead


@dataclass(eq=False)
class _Conn:
    ws: ServerConnection
    node_id: int
    name: str
    serial: int
    capabilities: tuple[str, ...]
    apps: tuple[str, ...]
    last_heard: float = field(default_factory=time.monotonic)
    pending: dict[int, asyncio.Future] = field(default_factory=dict)

    @property
    def target(self) -> Target:
        return Target(self.node_id, self.serial)


class NodeHub:
    def __init__(self, settings: Settings, node_settings: Callable[[], dict[str, dict]], host: str | None = None,
                 port: int | None = None, ping_every: float = m.PING_EVERY, ping_deadline: float = m.PING_DEADLINE) -> None:
        listen_host, _, listen_port = settings.node_listen.rpartition(":")
        self._settings, self._node_settings = settings, node_settings
        self._host = host or listen_host
        self._port = int(listen_port) if port is None else port
        self._ping_every, self._ping_deadline = ping_every, ping_deadline
        self._conns: list[_Conn] = []  # oldest first; the newest connection of a capability wins
        self._serials, self._call_ids = itertools.count(1), itertools.count(1)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="node-hub", daemon=True)
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._server = None
        self._db = None
        self._error: BaseException | None = None
        self.port = self._port

    # ---- lifecycle -------------------------------------------------------------------------------------

    def start(self, timeout: float = 10) -> None:
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("the node server did not start")
        if self._error:
            raise RuntimeError(f"the node server could not listen on {self._host}:{self._port}: {self._error}")
        log.info("node server listening on %s:%s", self._host, self.port)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._db = db.connect(self._settings)
            self._db.execute("UPDATE nodes SET connected_at = NULL")  # nothing is connected to a fresh server
            self._server = self._loop.run_until_complete(self._listen())
            self.port = self._server.sockets[0].getsockname()[1]
        except BaseException as e:
            self._error = e
            self._ready.set()
            return
        self._ready.set()
        self._loop.run_forever()
        self._db.close()

    async def _listen(self):
        return await serve(self._handle, self._host, self._port, ping_interval=None, max_size=MAX_FRAME)

    def stop(self) -> None:
        self._stopped.set()
        if not self._thread.is_alive():
            return

        async def shut() -> None:
            self._server.close()
            for conn in list(self._conns):
                await conn.ws.close(1001, "host stopping")
            await self._server.wait_closed()

        try:
            asyncio.run_coroutine_threadsafe(shut(), self._loop).result(10)
        except Exception:
            log.warning("the node server did not close cleanly", exc_info=True)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(10)

    # ---- the NodeLink the router uses ------------------------------------------------------------------

    def connected(self) -> list[NodeInfo]:
        return [NodeInfo(c.node_id, c.name, c.serial, c.capabilities, c.apps) for c in list(self._conns)]

    def is_live(self, target: Target) -> bool:
        return any(c.target == target for c in list(self._conns))

    def call(self, cap: str, app: str, handler: str, args: dict, timeout_s: float, *, as_arg: bool = False,
             target: Target | None = None) -> NodeReply:
        if self._stopped.is_set():  # the loop is gone, so a scheduled call would wait forever
            raise NodeError("The device disconnected." if target else f"That needs {cap}, and no device with it is connected.")
        future = asyncio.run_coroutine_threadsafe(
            self._call(cap, app, handler, args, timeout_s, as_arg, target), self._loop)
        return future.result()

    async def _call(self, cap: str, app: str, handler: str, args: dict, timeout_s: float, as_arg: bool,
                    target: Target | None) -> NodeReply:
        conn = self._pick(cap, app, target)
        call_id = next(self._call_ids)
        reply: asyncio.Future = self._loop.create_future()
        conn.pending[call_id] = reply
        try:
            await conn.ws.send(m.Call(call_id, app, handler, args, timeout_s, as_arg).to_json())
            result = await asyncio.wait_for(reply, timeout_s + ANSWER_GRACE)
        except asyncio.TimeoutError:
            raise NodeError(f'The device "{conn.name}" didn\'t answer within {int(timeout_s)} seconds.') from None
        except ConnectionClosed:
            raise NodeError(f'The device "{conn.name}" disconnected.') from None
        finally:
            conn.pending.pop(call_id, None)
        return _reply(conn, result)

    def _pick(self, cap: str, app: str, target: Target | None) -> _Conn:
        if target is not None:
            for c in self._conns:
                if c.target == target:
                    return c
            raise NodeError("The device disconnected.")
        for c in reversed(self._conns):  # most recently connected; presence-based choice comes later
            if cap in c.capabilities and app in c.apps:
                return c
        raise NodeError(f"That needs {cap}, and no device with it is connected.")

    # ---- one connection --------------------------------------------------------------------------------

    def _authenticate(self, hello: m.Hello) -> tuple[int | None, str]:
        """(node id, '') for a good hello, else (None, why)."""
        if hello.v != m.VERSION:
            return None, f"This host speaks protocol version {m.VERSION}, not {hello.v}."
        row = self._db.execute(
            "SELECT * FROM nodes WHERE name = ? ORDER BY revoked_at IS NULL DESC, id DESC", (hello.node,)).fetchone()
        if row is None or not hmac.compare_digest(row["key_hash"], hash_key(hello.key)):
            return None, "Unknown node or wrong key."
        if row["revoked_at"]:
            return None, "This node has been revoked."
        return row["id"], ""

    async def _reject(self, ws: ServerConnection, reason: str) -> None:
        log.warning("rejected a node: %s", reason)
        await ws.send(m.Reject(reason).to_json())
        await ws.close(1008, "rejected")

    async def _handle(self, ws: ServerConnection) -> None:
        try:
            first = await asyncio.wait_for(ws.recv(), HELLO_WAIT)
        except (asyncio.TimeoutError, ConnectionClosed):
            return
        hello = m.parse(first)
        if not isinstance(hello, m.Hello):
            await self._reject(ws, "Expected a hello first.")
            return
        node_id, why = self._authenticate(hello)
        if node_id is None:
            await self._reject(ws, why)
            return
        conn = _Conn(ws, node_id, hello.node, next(self._serials),
                     tuple(str(c) for c in hello.capabilities), tuple(str(a) for a in hello.apps))
        for old in [c for c in self._conns if c.name == conn.name]:  # one connection per name: the new one wins
            await old.ws.close(4000, "replaced by a newer connection")
        settings = {a: v for a, v in self._node_settings().items() if a in conn.apps}
        await ws.send(m.Welcome(node_id, settings).to_json())
        self._conns.append(conn)
        self._db.execute("UPDATE nodes SET connected_at=?, last_seen=?, capabilities_json=? WHERE id=?",
                         (stamp(), stamp(), json.dumps(list(conn.capabilities)), node_id))
        log.info("node %s connected (capabilities: %s)", conn.name, ", ".join(conn.capabilities) or "none")
        pinger = asyncio.create_task(self._ping(conn))
        try:
            await self._read(conn)
        finally:
            pinger.cancel()
            self._left(conn)

    async def _read(self, conn: _Conn) -> None:
        try:
            async for frame in conn.ws:
                conn.last_heard = time.monotonic()
                msg = m.parse(frame)
                if isinstance(msg, m.CallResult):
                    waiting = conn.pending.get(msg.id)
                    if waiting and not waiting.done():
                        waiting.set_result(msg)
                    else:
                        log.warning("node %s answered call %s, which nobody is waiting for", conn.name, msg.id)
                elif isinstance(msg, m.Event):
                    await self._event(conn, msg)
                elif isinstance(msg, m.Ping):
                    await conn.ws.send(m.Pong(msg.t).to_json())
                elif msg is not None and not isinstance(msg, m.Pong):
                    log.warning("node %s sent an unexpected %s; dropped", conn.name, msg.type)
        except ConnectionClosed:
            pass

    async def _event(self, conn: _Conn, msg: m.Event) -> None:
        """An app on the node speaks first: one outbox row (deduped on the event id), then the ack. An event from an
        app the node didn't announce is dropped, and acked so the node doesn't keep resending it."""
        if msg.app not in conn.apps:
            log.warning("node %s sent an event for %s, which it didn't announce; dropped", conn.name, msg.app)
        else:
            text = msg.text if len(msg.text) <= m.MAX_EVENT_TEXT else msg.text[: m.MAX_EVENT_TEXT - 1] + "…"
            outbox.add(self._db, text, f"app:{msg.app}", dedupe_key=f"event:{msg.id}")  # the sender polls the table
        await conn.ws.send(m.EventAck(msg.id).to_json())

    async def _ping(self, conn: _Conn) -> None:
        try:
            while True:
                await asyncio.sleep(self._ping_every)
                if time.monotonic() - conn.last_heard > self._ping_deadline:
                    log.warning("node %s went silent; dropping it", conn.name)
                    await conn.ws.close(1011, "silent")
                    return
                await conn.ws.send(m.Ping(time.time()).to_json())
        except ConnectionClosed:
            return

    def _left(self, conn: _Conn) -> None:
        if conn in self._conns:
            self._conns.remove(conn)
        for waiting in conn.pending.values():
            if not waiting.done():
                waiting.set_exception(NodeError(f'The device "{conn.name}" disconnected.'))
        if not any(c.node_id == conn.node_id for c in self._conns):  # a replacement may already be connected
            self._db.execute("UPDATE nodes SET connected_at = NULL, last_seen = ? WHERE id = ?", (stamp(), conn.node_id))
        log.info("node %s disconnected", conn.name)


def _reply(conn: _Conn, msg: m.CallResult) -> NodeReply:
    attachments = []
    total = 0
    for a in msg.attachments:
        try:
            raw = base64.b64decode(a["data"], validate=True)
            attachments.append((str(a["kind"]), str(a["mime"]), raw))
        except (KeyError, TypeError, binascii.Error):
            return NodeReply(conn.target, False, crash=True)
        total += len(raw)
    if total > m.MAX_ATTACHMENT_BYTES:
        return NodeReply(conn.target, False, error="The device sent back more than 10 MB of pictures.")
    return NodeReply(conn.target, msg.ok, msg.text, msg.data, msg.undo, msg.subject, tuple(attachments),
                     msg.error, msg.crash)

