"""Running actions: validate, decide whether to ask, execute, log, answer asks, undo."""

from __future__ import annotations

import json
import re
from collections.abc import Collection
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Union

from jarvis.contract.api import ActionError, Result
from jarvis.core import dispatch, sessions
from jarvis.core.apps import LoadedApp, Registry
from jarvis.core.timeutil import stamp
from jarvis.contract.manifest import ActionSpec
from jarvis.contract.schema import fill_defaults, validate

ASK_TTL = timedelta(hours=24)
NOT_LOGGED = "(not logged)"
LOG_TEXT_MAX = 500


@dataclass(frozen=True)
class Origin:
    channel: str
    outside_content: bool = False  # the turn was driven by a web page, email, file or screen text
    found_links: tuple[str, ...] = ()  # links that appeared in that outside content (normalized; see links_in)


FOUND_LINKS_MAX = 400
_URL = re.compile(r"https?://[^\s<>\"'`)\]]+", re.I)


def normal_link(url: str) -> str:
    """A link as compared: no fragment, no trailing slash or punctuation, scheme and host in lower case."""
    url = url.strip().split("#", 1)[0].rstrip(".,;:!?/")
    scheme, sep, rest = url.partition("://")
    host, slash, path = rest.partition("/")
    return f"{scheme.lower()}{sep}{host.lower()}{slash}{path}" if sep else url


def links_in(text: str, data: object = None) -> set[str]:
    """Every link in an action's text and data (a page's link list, a search result's urls)."""
    found = {normal_link(u) for u in _URL.findall(text or "")}

    def walk(value: object) -> None:
        if isinstance(value, str):
            if value.lower().startswith(("http://", "https://")):
                found.add(normal_link(value))
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                walk(v)

    walk(data)
    return found


def with_links(origin: Origin, links: set[str]) -> Origin:
    merged = tuple(dict.fromkeys([*origin.found_links, *sorted(links)]))[-FOUND_LINKS_MAX:]
    return replace(origin, outside_content=True, found_links=merged)


def follows_found_link(args: object, origin: Origin) -> bool:
    """The call only sends links that outside content itself showed: every text argument is one of them. Following a
    link from a page or a search result tells the site nothing the page didn't already hold; a search query or an
    address Falak wrote could carry what it read, so those still ask."""
    if not isinstance(args, dict):
        return False
    texts = [v for v in args.values() if isinstance(v, str)]
    found = set(origin.found_links)
    return bool(texts) and all(normal_link(t) in found for t in texts)


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


def needs_confirmation(effect: str, origin: Origin, trusted: bool, sends_data: bool = False,
                       args: object = None) -> bool:
    """The only place the ask-or-run decision is made. `trusted`: a learned rule allows this tool.
    `sends_data`: the arguments leave the device, so once outside content has steered the turn it asks
    (a page can't make Falak put what it read into a search or a URL), unless all it sends is a link that outside
    content showed (following a search result or a page's link)."""
    if effect in ("irreversible", "plan"):  # a learned rule never lets either run on its own
        return True
    if effect == "outbound":
        return not trusted or origin.outside_content
    return sends_data and origin.outside_content and not follows_found_link(args, origin)


def _log(reg: Registry, tool: str, effect: str, status: str, args: str, text: str, undo: dict | None = None,
         subject: str | None = None) -> int:
    cur = reg.conn.execute(
        "INSERT INTO action_log (time, tool, effect, status, args, result, undo_json, subject) VALUES (?,?,?,?,?,?,?,?)",
        (stamp(), tool, effect, status, args, text[:LOG_TEXT_MAX], json.dumps(undo) if undo is not None else None,
         subject),
    )
    return cur.lastrowid


def _args_text(action: ActionSpec, args: object) -> str:
    return json.dumps(args, default=str, ensure_ascii=False) if action.log_args else NOT_LOGGED


def _question_text(action: ActionSpec, question: str) -> str:
    return question if action.log_args else "(question not logged)"  # the question quotes the arguments


class _Fields(dict):
    def __missing__(self, key: str) -> str:
        return "(not given)"


def _plain(args: dict) -> _Fields:
    """What a confirm question shows for each argument: a list of words reads as "a; b", not as Python."""
    return _Fields({k: "; ".join(map(str, v)) if isinstance(v, list) and all(isinstance(x, str) for x in v) else v
                    for k, v in args.items()})


def _fail(reg: Registry, app: LoadedApp, action: ActionSpec, args: object, message: str) -> Failed:
    _log(reg, f"{app.manifest.name}__{action.name}", action.effect, "failed", _args_text(action, args), message)
    return Failed(message)


def _execute(reg: Registry, app: LoadedApp, action: ActionSpec, args: dict) -> Ran | Failed:
    try:
        if action.session:
            result = sessions.invoke(reg, app, action, args)
        else:
            result, _ = dispatch.call(reg, app, action.handler, args, action.timeout_s)
    except ActionError as e:
        return _fail(reg, app, action, args, str(e))
    except Exception:
        app.ctx.log.exception("action %s failed", action.name)
        return _fail(reg, app, action, args, f"Something went wrong in {app.manifest.name}")
    undo = result.undo if action.undo else None
    log_id = _log(reg, f"{app.manifest.name}__{action.name}", action.effect, "ran",
                  _args_text(action, args), result.text, undo, result.subject)
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
    if not needs_confirmation(action.effect, origin, tool_name in allowed, action.sends_data, args):
        return _execute(reg, app, action, args)
    question = action.confirm.format_map(_plain(args))
    cur = reg.conn.execute(
        "INSERT INTO asks (tool, args_json, question, status, created_at, origin_json) VALUES (?,?,?,?,?,?)",
        (tool_name, json.dumps(args), question, "pending", stamp(), json.dumps(asdict(origin))),
    )
    _log(reg, tool_name, action.effect, "asked", _args_text(action, args), _question_text(action, question))
    return Asked(cur.lastrowid, question)


def _expire(reg: Registry, now: datetime | None) -> None:
    cutoff = stamp((now or datetime.now(timezone.utc)) - ASK_TTL)
    reg.conn.execute(
        "UPDATE asks SET status='expired', resolved_at=? WHERE status='pending' AND created_at < ?",
        (stamp(now), cutoff),
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
        "UPDATE asks SET status=?, resolved_at=? WHERE id=? AND status='pending'", (status, stamp(now), ask_id)
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
    if row["subject"]:
        newer = reg.conn.execute(
            "SELECT 1 FROM action_log WHERE id > ? AND subject = ? AND status = 'ran' AND undone_at IS NULL",
            (row["id"], row["subject"]),
        ).fetchone()
        if newer:
            return Failed("A newer change to the same thing came after it; undo that one first.")
    try:
        result, _ = dispatch.call(reg, app, action.undo, json.loads(row["undo_json"]), action.timeout_s, as_arg=True)
    except ActionError as e:
        return Failed(str(e))
    except Exception:
        app.ctx.log.exception("undo of %s failed", action.name)
        return Failed(f"Something went wrong undoing that in {app.manifest.name}")
    reg.conn.execute("UPDATE action_log SET undone_at=? WHERE id=?", (stamp(), row["id"]))
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
