"""Host-node messages: JSON text frames, each with a `type` and the protocol version `v`. Standard library only."""

from __future__ import annotations

import json
import logging
from dataclasses import MISSING, asdict, dataclass, field, fields
from typing import ClassVar

VERSION = 1
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024  # raw bytes, all attachments of one result together
PING_EVERY = 20  # seconds, host to node
PING_DEADLINE = 60  # a node silent this long is dropped

_NONE = type(None)
log = logging.getLogger("jarvis.protocol")


class ProtocolError(Exception):
    pass


@dataclass(frozen=True)
class Message:
    type: ClassVar[str]
    _types: ClassVar[dict[str, tuple[type, ...]]] = {}  # field -> allowed JSON types; fields with a default may be absent

    def to_json(self) -> str:
        return json.dumps({"type": self.type, "v": VERSION, **asdict(self)})

    @classmethod
    def from_dict(cls, data: dict) -> "Message":
        values = {}
        for f in fields(cls):
            if f.name not in data:
                if f.default is MISSING and f.default_factory is MISSING:
                    raise ProtocolError(f"{cls.type}: missing '{f.name}'")
                continue
            value = data[f.name]
            allowed = cls._types[f.name]
            if not isinstance(value, allowed) or (bool not in allowed and isinstance(value, bool)):
                raise ProtocolError(f"{cls.type}: '{f.name}' has the wrong type")
            values[f.name] = value
        return cls(**values)


@dataclass(frozen=True)
class Hello(Message):
    type: ClassVar[str] = "hello"
    _types: ClassVar[dict] = {"node": (str,), "key": (str,), "capabilities": (list,), "apps": (list,), "v": (int,)}
    node: str
    key: str
    capabilities: list
    apps: list
    v: int = VERSION


@dataclass(frozen=True)
class Welcome(Message):
    type: ClassVar[str] = "welcome"
    _types: ClassVar[dict] = {"node_id": (int,), "settings": (dict,)}
    node_id: int
    settings: dict  # app name -> merged, validated settings, for the apps this node runs


@dataclass(frozen=True)
class Reject(Message):
    type: ClassVar[str] = "reject"
    _types: ClassVar[dict] = {"reason": (str,)}
    reason: str


@dataclass(frozen=True)
class Call(Message):
    type: ClassVar[str] = "call"
    _types: ClassVar[dict] = {"id": (int,), "app": (str,), "handler": (str,), "args": (dict,),
                              "timeout_s": (int, float), "as_arg": (bool,)}
    id: int
    app: str
    handler: str
    args: dict  # already validated by the host
    timeout_s: float
    as_arg: bool = False  # pass `args` as one dict argument (undo, sample, stop) instead of keywords


@dataclass(frozen=True)
class WireAttachment:
    kind: str
    mime: str
    data: str  # base64


@dataclass(frozen=True)
class CallResult(Message):
    type: ClassVar[str] = "result"
    _types: ClassVar[dict] = {"id": (int,), "ok": (bool,), "text": (str,), "data": (dict, _NONE),
                              "undo": (dict, _NONE), "subject": (str, _NONE), "attachments": (list,),
                              "error": (str, _NONE), "crash": (bool,)}
    id: int
    ok: bool
    text: str = ""
    data: dict | None = None
    undo: dict | None = None
    subject: str | None = None
    attachments: list = field(default_factory=list)  # [{"kind", "mime", "data"}] with base64 data
    error: str | None = None  # an ActionError's message
    crash: bool = False  # any other exception; the details stay in the node's log


@dataclass(frozen=True)
class Ping(Message):
    type: ClassVar[str] = "ping"
    _types: ClassVar[dict] = {"t": (int, float)}
    t: float


@dataclass(frozen=True)
class Pong(Message):
    type: ClassVar[str] = "pong"
    _types: ClassVar[dict] = {"t": (int, float)}
    t: float


MAX_EVENT_TEXT = 4000


@dataclass(frozen=True)
class Event(Message):
    """Node to host: an app on the node says something to Elijah on its own (a job finished, a task is done)."""

    type: ClassVar[str] = "event"
    _types: ClassVar[dict] = {"id": (str,), "app": (str,), "text": (str,)}
    id: str  # a uuid the node keeps until the host acks it; the host dedupes on it
    app: str
    text: str


@dataclass(frozen=True)
class EventAck(Message):
    """Host to node: the event is in the host's outbox, so the node may forget it."""

    type: ClassVar[str] = "event_ack"
    _types: ClassVar[dict] = {"id": (str,)}
    id: str


_BY_TYPE: dict[str, type[Message]] = {
    c.type: c for c in (Hello, Welcome, Reject, Call, CallResult, Ping, Pong, Event, EventAck)}


def from_json(text: str | bytes) -> Message:
    """Parse one frame. Raises ProtocolError for anything that is not a well-formed known message."""
    try:
        data = json.loads(text)
    except (ValueError, UnicodeDecodeError) as e:
        raise ProtocolError(f"not JSON: {e}") from None
    if not isinstance(data, dict):
        raise ProtocolError("a frame must be a JSON object")
    cls = _BY_TYPE.get(data.get("type"))
    if cls is None:
        raise ProtocolError(f"unknown message type {data.get('type')!r}")
    return cls.from_dict(data)


def parse(text: str | bytes) -> Message | None:
    """Like from_json, but a bad frame is logged and dropped (None), so neither side can be crashed by one."""
    try:
        return from_json(text)
    except ProtocolError as e:
        log.warning("dropped a frame: %s", e)
        return None
