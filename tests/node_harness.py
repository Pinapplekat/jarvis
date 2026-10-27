"""A real host hub and a real node client over a localhost WebSocket, the node running in its own thread."""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

from jarvis.core import nodes
from jarvis.core.apps import node_app_settings
from jarvis.core.node_hub import NodeHub
from jarvis.node.client import NodeClient
from jarvis.node.config import NodeConfig
from jarvis.node.runtime import NodeRuntime

FIXTURES = Path(__file__).parent / "fixtures" / "apps"


def wait_for(check, timeout=5.0, what="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


def start_hub(reg, **kw) -> NodeHub:
    hub = NodeHub(reg.settings, lambda: node_app_settings(reg), host="127.0.0.1", port=kw.pop("port", 0), **kw)
    hub.start()
    reg.nodes = hub
    return hub


class NodeThread:
    """A node (the `eye` fixture app, or others in `apps_dir`) that connects to `hub` until stopped."""

    def __init__(self, tmp_path: Path, port: int, key: str, name: str = "mac", apps_dir: Path = FIXTURES,
                 backoff=(0.05, 0.1)) -> None:
        folder = tmp_path / f"node-{name}"
        folder.mkdir(exist_ok=True)
        self.config = NodeConfig(folder, name, f"ws://127.0.0.1:{port}", key)
        self.runtime = NodeRuntime(folder, self.config.tmp_dir, apps_dir=apps_dir)
        self.client = NodeClient(self.config, self.runtime, backoff)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._loop.run_until_complete(self.client.run())
        self._loop.close()

    def start(self) -> "NodeThread":
        self._thread.start()
        return self

    def wait_connected(self, timeout=5.0) -> None:
        if not self.client.connected.wait(timeout):
            raise AssertionError("the node did not connect")

    def stop(self) -> None:
        wait_for(lambda: self._loop is not None, what="loop")
        if self._thread.is_alive():
            try:
                self._loop.call_soon_threadsafe(self.client.stop)
            except RuntimeError:  # the loop finished between the check and the call
                pass
        self._thread.join(10)


def pair(reg, name="mac") -> str:
    return nodes.pair(reg.conn, name)


def fake_node(port: int, key: str, name: str = "mac", caps=("eye",), apps=("eye",), v: int = 1):
    """A hand-driven node: connect, say hello, return (websocket, first reply). The test decides what happens next."""
    from websockets.sync.client import connect

    from jarvis.protocol import messages as m

    ws = connect(f"ws://127.0.0.1:{port}", max_size=None)
    ws.send(m.Hello(name, key, list(caps), list(apps), v).to_json())
    return ws, m.parse(ws.recv(timeout=5))


def eye_module():
    """The eye fixture app's code as the node imported it (the host never imports node apps)."""
    return sys.modules["falak_app_eye_actions"]
