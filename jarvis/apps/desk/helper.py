"""The link to 'Falak Ears.app', the small Swift program that owns the microphone and the speakers.

It speaks length-prefixed frames over a Unix socket: one type byte, a 4-byte big-endian length, the payload. Type 1
is audio (16 kHz PCM16 from the microphone going out, 24 kHz PCM16 for the speakers going in); type 2 is one line of
JSON (commands in, events out). The app is started through LaunchServices (`open`), which is what makes macOS ask for
the Microphone permission for Falak Ears and not for whatever Python runs under. The node listens on a socket in a
private folder and the app connects to it, so the audio never touches a disk. (Pointing `open --stdin/--stdout` at
named pipes hung LaunchServices, since opening a pipe waits for its other end, and no other app could open.)"""

from __future__ import annotations

import json
import logging
import os
import socket
import struct
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("jarvis.apps.desk.helper")

AUDIO, JSON = 1, 2
_HEAD = struct.Struct(">BI")
MAX_FRAME = 1 << 20


def encode(kind: int, payload: bytes) -> bytes:
    return _HEAD.pack(kind, len(payload)) + payload


def read_frames(read: Callable[[int], bytes], on_audio: Callable[[bytes], None], on_event: Callable[[dict], None]) -> None:
    """Pump frames from `read(n)` (returns b"" at the end) until the stream closes."""
    def exactly(n: int) -> bytes | None:
        data = b""
        while len(data) < n:
            part = read(n - len(data))
            if not part:
                return None
            data += part
        return data

    while (head := exactly(_HEAD.size)) is not None:
        kind, length = _HEAD.unpack(head)
        if length > MAX_FRAME or kind not in (AUDIO, JSON):
            log.error("the desk helper sent a bad frame (type %d, %d bytes); stopping", kind, length)
            return
        payload = exactly(length) if length else b""
        if payload is None:
            return
        if kind == AUDIO:
            on_audio(payload)
        else:
            try:
                on_event(json.loads(payload))
            except ValueError:
                log.warning("the desk helper sent unreadable JSON")


class PipeHelper:
    """A helper whose frames travel over a pair of binary streams (a subprocess's pipes, or named pipes)."""

    def __init__(self, write: Callable[[bytes], None], close: Callable[[], None]) -> None:
        self._write, self._close, self._lock = write, close, threading.Lock()

    def _send(self, kind: int, payload: bytes) -> None:
        with self._lock:
            try:
                self._write(encode(kind, payload))
            except (OSError, ValueError):
                log.warning("the desk helper is gone")

    def play(self, pcm: bytes) -> None:
        self._send(AUDIO, pcm)

    def command(self, cmd: dict) -> None:
        self._send(JSON, json.dumps(cmd).encode() + b"\n")

    def close(self) -> None:
        self.command({"cmd": "quit"})
        self._close()


OPEN_TIMEOUT = 15  # seconds for `open` itself; it returns as soon as the app is launched
CONNECT_TIMEOUT = 30  # seconds for the app to connect back


def launch(app: Path, run_dir: Path, on_audio: Callable[[bytes], None], on_event: Callable[[dict], None],
           connect_timeout: float = CONNECT_TIMEOUT, args: tuple[str, ...] = ()) -> PipeHelper:
    """Open Falak Ears.app in the background and wait for it to connect to a socket in a private temp folder (a
    socket path is capped at 104 bytes, which deeper folders can pass). The socket is gone once the app joins."""
    for old in ("to-app.pipe", "from-app.pipe"):  # left in `run_dir` by the named-pipe version
        (run_dir / old).unlink(missing_ok=True)
    private = Path(tempfile.mkdtemp(prefix="falak-ears-"))  # 0700
    path = private / "s"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(path))
        os.chmod(path, 0o600)
        server.listen(1)
        server.settimeout(connect_timeout)
        try:
            result = subprocess.run(["/usr/bin/open", "-g", "-n", "-a", str(app), "--args", "--socket", str(path), *args],
                                    capture_output=True, text=True, timeout=OPEN_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise RuntimeError("couldn't open Falak Ears (macOS didn't launch it in time)") from None
        if result.returncode != 0:
            raise RuntimeError(f"couldn't open Falak Ears ({result.stderr.strip()[:120] or result.returncode})")
        try:
            conn, _ = server.accept()
        except socket.timeout:
            raise RuntimeError("Falak Ears opened but didn't connect") from None
    finally:
        server.close()
        shutil.rmtree(private, ignore_errors=True)
    conn.settimeout(None)

    def close() -> None:
        try:
            conn.close()
        except OSError:
            pass

    threading.Thread(target=read_frames, args=(lambda n: _recv(conn, n), on_audio, on_event),
                     name="desk-helper", daemon=True).start()
    return PipeHelper(conn.sendall, close)


def _recv(conn: socket.socket, n: int) -> bytes:
    try:
        return conn.recv(n)
    except OSError:
        return b""
