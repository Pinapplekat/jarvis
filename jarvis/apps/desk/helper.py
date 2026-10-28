"""The link to 'Falak Ears.app', the small Swift program that owns the microphone and the speakers.

It speaks length-prefixed frames over its stdin and stdout: one type byte, a 4-byte big-endian length, the payload.
Type 1 is audio (16 kHz PCM16 from the microphone going out, 24 kHz PCM16 for the speakers going in); type 2 is one
line of JSON (commands in, events out). The app is started through LaunchServices (`open`), which is what makes macOS
ask for the Microphone permission for Falak Ears and not for whatever Python runs under; `open --stdin/--stdout` can
point its standard streams at named pipes in a private folder, so the audio never touches a disk."""

from __future__ import annotations

import json
import logging
import os
import struct
import subprocess
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


def launch(app: Path, run_dir: Path, on_audio: Callable[[bytes], None], on_event: Callable[[dict], None]) -> PipeHelper:
    """Open Falak Ears.app with its standard streams on two named pipes under `run_dir` (0700) and start reading."""
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    to_app, from_app = run_dir / "to-app.pipe", run_dir / "from-app.pipe"
    for pipe in (to_app, from_app):
        pipe.unlink(missing_ok=True)
        os.mkfifo(pipe, 0o600)
    result = subprocess.run(["/usr/bin/open", "-g", "-n", "-a", str(app), "--stdin", str(to_app), "--stdout",
                             str(from_app), "--stderr", str(run_dir / "app.log")], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"couldn't open Falak Ears ({result.stderr.strip()[:120] or result.returncode})")
    reader = os.open(from_app, os.O_RDONLY)  # blocks until the app opens its end
    writer = os.open(to_app, os.O_WRONLY)

    def close() -> None:
        for fd in (writer, reader):
            try:
                os.close(fd)
            except OSError:
                pass
        for pipe in (to_app, from_app):
            pipe.unlink(missing_ok=True)

    threading.Thread(target=read_frames, args=(lambda n: os.read(reader, n), on_audio, on_event),
                     name="desk-helper", daemon=True).start()
    return PipeHelper(lambda data: _write_all(writer, data), close)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]
