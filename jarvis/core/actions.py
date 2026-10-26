"""Running actions: validate, decide whether to ask, execute, log, answer asks, undo."""

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Union

from jarvis.core.api import ActionError, Result
from jarvis.core.apps import LoadedApp, Registry, run_on_this_machine, tmp_dir
from jarvis.core.manifest import ActionSpec
from jarvis.core.schema import fill_defaults, validate

ASK_TTL = timedelta(hours=24)
NOT_LOGGED = "(not logged)"
LOG_TEXT_MAX = 500


@dataclass(frozen=True)
class Origin:
    channel: str
    outside_content: bool = False  # the turn was driven by a web page, email, file or screen text


@dataclass(frozen=True)
class Ran:
    result: Result
    log_id: int


@dataclass(frozen=True)
class Asked:
    ask_id: int
    question: str


@dataclass(frozen=True)
class Failed:
    message: str


@dataclass(frozen=True)
class Declined:
    ask_id: int


Outcome = Union[Ran, Asked, Failed, Declined]


def needs_confirmation(effect: str, origin: Origin, trusted: bool) -> bool:
    """The only place the ask-or-run decision is made. `trusted`: a learned rule allows this tool."""
    if effect == "irreversible":
        return True
    if effect == "outbound":
        return not trusted or origin.outside_content
    return False


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")


def _log(reg: Registry, tool: str, effect: str, status: str, args: str, text: str, undo: dict | None = None) -> int:
    cur = reg.conn.execute(
        "INSERT INTO action_log (time, tool, effect, status, args, result, undo_json) VALUES (?,?,?,?,?,?,?)",
        (_stamp(), tool, effect, status, args, text[:LOG_TEXT_MAX], json.dumps(undo) if undo is not None else None),
    )
    return cur.lastrowid


def _args_text(action: ActionSpec, args: object) -> str:
    return json.dumps(args, default=str, ensure_ascii=False) if action.log_args else NOT_LOGGED


def _question_text(action: ActionSpec, question: str) -> str:
    return question if action.log_args else "(question not logged)"  # the question quotes the arguments


class _Fields(dict):
    def __missing__(self, key: str) -> str:
        return "(not given)"


def _fail(reg: Registry, app: LoadedApp, action: ActionSpec, args: object, message: str) -> Failed:
    _log(reg, f"{app.manifest.name}__{action.name}", action.effect, "failed", _args_text(action, args), message)
    return Failed(message)


def _execute(reg: Registry, app: LoadedApp, action: ActionSpec, args: dict) -> Ran | Failed:
    if not run_on_this_machine(app.manifest.runs_on):
        return _fail(reg, app, action, args, f"{app.manifest.name} needs a device that isn't connected")
    try:
        result = getattr(app.module, action.handler)(app.ctx, **args)
        if not isinstance(result, Result):
            raise TypeError(f"handler returned {type(result).__name__}, not Result")
        tmp = tmp_dir(reg.settings).resolve()
        for att in result.attachments:
            if att.path.resolve().parent != tmp:  # the core deletes attachments, so only its own temp files
                raise ValueError(f"attachment {att.path} is outside the temp folder")
    except ActionError as e:
        return _fail(reg, app, action, args, str(e))
    except Exception:
        app.ctx.log.exception("action %s failed", action.name)
        return _fail(reg, app, action, args, f"Something went wrong in {app.manifest.name}")
    undo = result.undo if action.undo else None
    log_id = _log(reg, f"{app.manifest.name}__{action.name}", action.effect, "ran",
                  _args_text(action, args), result.text, undo)
    return Ran(result, log_id)


def run(reg: Registry, tool_name: str, args: object, origin: Origin,
        allowed: Collection[str] = frozenset()) -> Ran | Asked | Failed:
    """Run one action. `allowed` holds tool names a learned trust rule lets run without asking (empty in M2)."""
    found = reg.find(tool_name)
    if found is None:
        return Failed(f"There is no action called {tool_name}.")
    app, action = found
    args = fill_defaults(action.input, args)
    problems = validate(action.input, args)
    if problems:
        return _fail(reg, app, action, args, "Invalid input: " + "; ".join(problems))
    if not needs_confirmation(action.effect, origin, tool_name in allowed):
        return _execute(reg, app, action, args)
    question = action.confirm.format_map(_Fields(args))
    cur = reg.conn.execute(
        "INSERT INTO asks (tool, args_json, question, status, created_at, origin_json) VALUES (?,?,?,?,?,?)",
        (tool_name, json.dumps(args), question, "pending", _stamp(), json.dumps(asdict(origin))),
    )
    _log(reg, tool_name, action.effect, "asked", _args_text(action, args), _question_text(action, question))
    return Asked(cur.lastrowid, question)


def _expire(reg: Registry, now: datetime | None) -> None:
    cutoff = _stamp((now or datetime.now(timezone.utc)) - ASK_TTL)
    reg.conn.execute(
        "UPDATE asks SET status='expired', resolved_at=? WHERE status='pending' AND created_at < ?",
        (_stamp(now), cutoff),
    )


def pending_asks(reg: Registry, now: datetime | None = None) -> list[dict]:
    _expire(reg, now)
    return [dict(r) for r in reg.conn.execute("SELECT * FROM asks WHERE status='pending' ORDER BY id")]


def answer_ask(reg: Registry, ask_id: int, yes: bool, now: datetime | None = None) -> Ran | Failed | Declined:
    """Run exactly the stored arguments if the ask is still pending and `yes`; otherwise decline it."""
    _expire(reg, now)
    row = reg.conn.execute("SELECT * FROM asks WHERE id=?", (ask_id,)).fetchone()
    if row is None:
        return Failed(f"There is no ask #{ask_id}.")
    if row["status"] != "pending":
        return Failed(f"Ask #{ask_id} can't be answered: it is already {row['status']}.")
    found = reg.find(row["tool"])
    status = "approved" if yes else "declined"
    changed = reg.conn.execute(
        "UPDATE asks SET status=?, resolved_at=? WHERE id=? AND status='pending'", (status, _stamp(now), ask_id)
    ).rowcount
    if not changed:
        return Failed(f"Ask #{ask_id} was already answered.")
    if found is None:
        return Failed(f"{row['tool']} is no longer available.")
    app, action = found
    args = json.loads(row["args_json"])
    if not yes:
        _log(reg, row["tool"], action.effect, "declined", _args_text(action, args),
             _question_text(action, row["question"]))
        return Declined(ask_id)
    return _execute(reg, app, action, args)


def undo(reg: Registry, log_id: int | None = None) -> Ran | Failed:
    """Undo one log entry, or the most recent undoable one that hasn't been undone."""
    if log_id is None:
        row = reg.conn.execute(
            "SELECT * FROM action_log WHERE status='ran' AND undo_json IS NOT NULL AND undone_at IS NULL "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return Failed("There is nothing to undo.")
    else:
        row = reg.conn.execute("SELECT * FROM action_log WHERE id=?", (log_id,)).fetchone()
        if row is None:
            return Failed(f"There is no log entry #{log_id}.")
        if row["undone_at"]:
            return Failed(f"Entry #{log_id} was already undone.")
        if row["status"] != "ran" or row["undo_json"] is None:
            return Failed(f"Entry #{log_id} can't be undone.")
    found = reg.find(row["tool"])
    if found is None or found[1].undo is None:
        return Failed(f"{row['tool']} can't be undone any more.")
    app, action = found
    try:
        result = getattr(app.module, action.undo)(app.ctx, json.loads(row["undo_json"]))
    except ActionError as e:
        return Failed(str(e))
    except Exception:
        app.ctx.log.exception("undo of %s failed", action.name)
        return Failed(f"Something went wrong undoing that in {app.manifest.name}")
    reg.conn.execute("UPDATE action_log SET undone_at=? WHERE id=?", (_stamp(), row["id"]))
    new_id = _log(reg, f"{row['tool']} (undo)", action.effect, "ran", f"entry #{row['id']}", result.text)
    return Ran(result, new_id)


def recent_log(reg: Registry, n: int = 20) -> list[dict]:
    rows = reg.conn.execute("SELECT * FROM action_log ORDER BY id DESC LIMIT ?", (n,))
    return [dict(r) for r in rows][::-1]


def release(outcome: object) -> None:
    """Delete an outcome's attachment files. The caller does this once it has used them."""
    if isinstance(outcome, Ran):
        for att in outcome.result.attachments:
            att.path.unlink(missing_ok=True)
