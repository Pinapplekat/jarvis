"""Where a handler runs: in this process for `host` apps, on a connected device for `node:<cap>` apps."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from jarvis.contract.api import ActionError, Attachment, Result, SessionStarted
from jarvis.core.apps import LoadedApp, Registry, tmp_dir
from jarvis.protocol.messages import MAX_ATTACHMENT_BYTES


class NodeError(ActionError):
    """The device could not do it: not connected, too slow, or it dropped. The message says which, in plain words."""


@dataclass(frozen=True)
class Target:
    """One connection of one node. A session's live resource exists only on the connection that started it."""

    node_id: int
    conn: int


@dataclass(frozen=True)
class NodeReply:
    target: Target
    ok: bool
    text: str = ""
    data: dict | None = None
    undo: dict | None = None
    subject: str | None = None
    attachments: tuple[tuple[str, str, bytes], ...] = ()  # (kind, mime, bytes)
    error: str | None = None
    crash: bool = False


@dataclass(frozen=True)
class NodeInfo:
    node_id: int
    name: str
    conn: int
    capabilities: tuple[str, ...]
    apps: tuple[str, ...]


class NodeLink(Protocol):
    """What the router needs from the server that device nodes connect to."""

    def call(self, cap: str, app: str, handler: str, args: dict, timeout_s: float, *, as_arg: bool = False,
             target: Target | None = None) -> NodeReply: ...

    def is_live(self, target: Target) -> bool: ...

    def connected(self) -> list[NodeInfo]: ...


def _host_attachments(reg: Registry, result: Result) -> None:
    tmp = tmp_dir(reg.settings).resolve()
    for att in result.attachments:
        if att.path.resolve().parent != tmp:  # the core deletes attachments, so only its own temp files
            raise ValueError(f"attachment {att.path} is outside the temp folder")


def _from_node(reg: Registry, app: LoadedApp, reply: NodeReply) -> Result:
    if reply.crash:
        raise RuntimeError(f"{app.manifest.name} crashed on the device (see the node's log)")
    if not reply.ok:
        raise ActionError(reply.error or f"{app.manifest.name} failed on the device")
    if sum(len(b) for _, _, b in reply.attachments) > MAX_ATTACHMENT_BYTES:
        raise ActionError("The device sent back more than 10 MB of pictures.")
    saved: list[Attachment] = []
    try:
        for kind, mime, raw in reply.attachments:
            fd, name = _mkstemp(reg, mime)
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
            saved.append(Attachment(kind, Path(name), mime))
    except BaseException:
        for a in saved:
            a.path.unlink(missing_ok=True)
        raise
    return Result(reply.text, reply.data, reply.undo, tuple(saved), reply.subject)


def _mkstemp(reg: Registry, mime: str) -> tuple[int, str]:
    suffix = {"image/jpeg": ".jpg", "image/png": ".png"}.get(mime, ".bin")
    return tempfile.mkstemp(suffix=suffix, dir=tmp_dir(reg.settings))


def _coerce(out: object, start: bool) -> Result:
    if start:
        if not isinstance(out, SessionStarted):
            raise TypeError(f"start handler returned {type(out).__name__}, not SessionStarted")
        return Result(out.text, {"state": out.state})
    if not isinstance(out, Result):
        raise TypeError(f"handler returned {type(out).__name__}, not Result")
    return out


def call(reg: Registry, app: LoadedApp, handler: str, args: dict, timeout_s: float, *, as_arg: bool = False,
         start: bool = False, target: Target | None = None) -> tuple[Result, Target | None]:
    """Run one handler where its app lives. Raises ActionError for things to tell Elijah, anything else for bugs.

    `as_arg`: pass `args` as one dict (undo, sample, stop) rather than as keywords.
    `start`: the handler is a session start, which returns SessionStarted.
    `target`: for a session, the node connection that holds its live resource.
    """
    runs_on = app.manifest.runs_on
    if runs_on == "host":
        fn = getattr(app.module, handler)
        out = fn(app.ctx, args) if as_arg else fn(app.ctx, **args)
        result = _coerce(out, start)
        _host_attachments(reg, result)
        return result, None
    if reg.nodes is None:
        raise NodeError(_missing(runs_on))
    reply = reg.nodes.call(runs_on.split(":", 1)[1], app.manifest.name, handler, args, timeout_s,
                           as_arg=as_arg, target=target)
    result = _from_node(reg, app, reply)
    if start and not (result.data and isinstance(result.data.get("state"), dict)):
        for a in result.attachments:
            a.path.unlink(missing_ok=True)
        raise TypeError("the device's session start came back without state")
    return result, reply.target


def _missing(runs_on: str) -> str:
    return f"That needs {runs_on.split(':', 1)[1]}, and no device with it is connected."

