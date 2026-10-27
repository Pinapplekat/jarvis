import threading
import time

from jarvis.contract.api import ActionError, AppContext, Attachment, Result, SessionStarted

LIVE: dict[int, dict] = {}  # what the pretend camera is doing, by id; lives in the node process
COUNT = [0]
RELEASE = threading.Event()  # lets a slow call finish
_next = [0]


def _picture(ctx: AppContext, data: bytes) -> Attachment:
    path = ctx.temp_path(".jpg")
    path.write_bytes(data)
    return Attachment("image", path, "image/jpeg")


def snap(ctx: AppContext) -> Result:
    return Result(f"snap {ctx.settings['label']}", attachments=(_picture(ctx, b"jpeg-bytes"),))


def hello(ctx: AppContext) -> Result:
    return Result(f"hello from the node ({ctx.settings['label']})")


def fail(ctx: AppContext) -> Result:
    raise ActionError("The eye is shut.")


def boom(ctx: AppContext) -> Result:
    raise ValueError("secret detail")


def slow(ctx: AppContext) -> Result:
    RELEASE.wait(10)
    return Result("finally", attachments=(_picture(ctx, b"late"),))


def bump(ctx: AppContext) -> Result:
    COUNT[0] += 1
    return Result(f"count {COUNT[0]}", undo={"to": COUNT[0] - 1}, subject="count")


def unbump(ctx: AppContext, undo: dict) -> Result:
    COUNT[0] = undo["to"]
    return Result(f"count back to {COUNT[0]}")


def db_peek(ctx: AppContext) -> Result:
    ctx.db.execute("SELECT 1")
    return Result("db")


def start_live(ctx: AppContext, name: str = "front") -> SessionStarted:
    _next[0] += 1
    LIVE[_next[0]] = {"name": name, "looks": 0}
    return SessionStarted(f"Watching {name}.", {"id": _next[0], "name": name})


def sample_live(ctx: AppContext, state: dict) -> Result:
    live = LIVE.get(state["id"])
    if live is None:
        raise ActionError("The view is gone.")
    live["looks"] += 1
    return Result(f"look {live['looks']} of {live['name']}", attachments=(_picture(ctx, f"frame-{live['looks']}".encode()),))


def stop_live(ctx: AppContext, state: dict) -> Result:
    LIVE.pop(state["id"], None)
    return Result("closed")
