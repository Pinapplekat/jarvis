"""Live views: the host keeps the session table, the device keeps the running capture.

A session is three tools the model sees (<name>_start, _look, _stop). It ends when the model stops it, when it
sits unlooked-at past its idle time, when its device disconnects, or when the host exits; the reason is recorded.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from jarvis.contract.api import ActionError, Result
from jarvis.contract.manifest import ActionSpec, SessionSpec
from jarvis.core import dispatch
from jarvis.core.apps import LoadedApp, Registry
from jarvis.core.dispatch import Target
from jarvis.core.timeutil import ago, stamp

log = logging.getLogger("jarvis.core.sessions")


def _spec(app: LoadedApp, name: str) -> SessionSpec:
    return next(s for s in app.manifest.sessions if s.name == name)


def _open_rows(reg: Registry, app: str | None = None, name: str | None = None) -> list:
    sql, params = "SELECT * FROM sessions WHERE stopped_at IS NULL", []
    for column, value in (("app", app), ("name", name)):
        if value is not None:
            sql += f" AND {column} = ?"
            params.append(value)
    return reg.conn.execute(sql + " ORDER BY id", params).fetchall()


def _target(row) -> Target | None:
    return None if row["node_id"] is None else Target(row["node_id"], row["node_conn"])


def _alive(reg: Registry, row) -> bool:
    target = _target(row)
    return target is None or (reg.nodes is not None and reg.nodes.is_live(target))


def _end(reg: Registry, row, reason: str, app: LoadedApp | None) -> Result | None:
    """Mark a session stopped (first, so a failing stop handler can't leave it open), then let the device clean up."""
    reg.conn.execute("UPDATE sessions SET stopped_at=?, stop_reason=? WHERE id=?", (stamp(), reason, row["id"]))
    log.info("session #%s (%s %s) ended: %s", row["id"], row["app"], row["name"], reason)
    if app is None or not _alive(reg, row):
        return None
    spec = _spec(app, row["name"])
    try:
        result, _ = dispatch.call(reg, app, spec.stop, json.loads(row["state_json"]), spec.timeout_s,
                                  as_arg=True, target=_target(row))
        return result
    except Exception:
        log.warning("the stop handler of session #%s failed", row["id"], exc_info=True)
        return None


def _release(result: Result | None) -> None:
    for att in result.attachments if result else ():
        att.path.unlink(missing_ok=True)


def _require_open(reg: Registry, app: LoadedApp, spec: SessionSpec, session_id: int):
    row = reg.conn.execute("SELECT * FROM sessions WHERE id=? AND app=? AND name=?",
                           (session_id, app.manifest.name, spec.name)).fetchone()
    if row is None:
        raise ActionError(f"There is no {spec.name} session #{session_id}.")
    if row["stopped_at"]:
        raise ActionError(f"The {spec.name} session #{session_id} is closed ({row['stop_reason']}).")
    return row


def _start(reg: Registry, app: LoadedApp, spec: SessionSpec, action: ActionSpec, args: dict) -> Result:
    for row in _open_rows(reg, app.manifest.name, spec.name):
        raise ActionError(f"A {spec.name} is already open (#{row['id']}). Look at it, or close it first.")
    started, target = dispatch.call(reg, app, action.handler, args, action.timeout_s, start=True)
    state = started.data["state"]
    cur = reg.conn.execute(
        "INSERT INTO sessions (app, name, node_id, node_conn, state_json, started_at) VALUES (?,?,?,?,?,?)",
        (app.manifest.name, spec.name, target.node_id if target else None, target.conn if target else None,
         json.dumps(state), stamp()))
    sid = cur.lastrowid
    text = f"Opened {app.manifest.name} {spec.name} #{sid}. {started.text}".strip()
    try:
        look, _ = dispatch.call(reg, app, spec.sample, state, spec.timeout_s, as_arg=True, target=target)
    except Exception as e:  # the view is open; the first frame can come with the next look
        log.warning("first look of session #%s failed: %s", sid, e)
        why = str(e) if isinstance(e, ActionError) else "something went wrong"
        return Result(f"{text} The first look failed ({why}); try {spec.name}_look.", {"session_id": sid})
    reg.conn.execute("UPDATE sessions SET last_look_at=? WHERE id=?", (stamp(), sid))
    return Result(f"{text} {look.text}".strip(), {"session_id": sid}, attachments=look.attachments)


def _look(reg: Registry, app: LoadedApp, spec: SessionSpec, action: ActionSpec, args: dict) -> Result:
    row = _require_open(reg, app, spec, args["session_id"])
    if not _alive(reg, row):
        _end(reg, row, "node disconnected", None)
        raise ActionError(f"The {spec.name} closed because its device disconnected. Open it again when it's back.")
    look, _ = dispatch.call(reg, app, action.handler, json.loads(row["state_json"]), action.timeout_s,
                            as_arg=True, target=_target(row))
    reg.conn.execute("UPDATE sessions SET last_look_at=? WHERE id=?", (stamp(), row["id"]))
    return Result(look.text, {"session_id": row["id"]}, attachments=look.attachments)


def _stop(reg: Registry, app: LoadedApp, spec: SessionSpec, args: dict) -> Result:
    row = _require_open(reg, app, spec, args["session_id"])
    result = _end(reg, row, "stopped", app)
    _release(result)
    return Result(f"Closed {app.manifest.name} {spec.name} #{row['id']}.", {"session_id": row["id"]})


def invoke(reg: Registry, app: LoadedApp, action: ActionSpec, args: dict) -> Result:
    """Run one of a session's generated tools. Raises ActionError for things to tell Elijah."""
    spec = _spec(app, action.session)
    if action.phase == "start":
        return _start(reg, app, spec, action, args)
    if action.phase == "look":
        return _look(reg, app, spec, action, args)
    return _stop(reg, app, spec, args)


def _idle_for(row, now: datetime) -> timedelta:
    last = datetime.fromisoformat(row["last_look_at"] or row["started_at"])
    return now - last


def sweep(reg: Registry, now: datetime | None = None) -> int:
    """Close sessions whose device left or that have sat idle too long. Returns how many were closed."""
    now = now or datetime.now(timezone.utc)
    closed = 0
    for row in _open_rows(reg):
        app = reg.apps.get(row["app"])
        if app is None or not any(s.name == row["name"] for s in app.manifest.sessions):
            reason = "app unavailable"
        elif not _alive(reg, row):
            reason = "node disconnected"
        elif _idle_for(row, now) > timedelta(minutes=_spec(app, row["name"]).idle_minutes):
            reason = "idle"
        else:
            continue
        _release(_end(reg, row, reason, app))
        closed += 1
    return closed


def stop_all(reg: Registry, reason: str) -> int:
    rows = _open_rows(reg)
    for row in rows:
        app = reg.apps.get(row["app"])
        _release(_end(reg, row, reason, app))
    return len(rows)


def abandon_open(reg: Registry, reason: str) -> int:
    """Rows left open by a host that died: nothing can be live, so just record the end."""
    return reg.conn.execute("UPDATE sessions SET stopped_at=?, stop_reason=? WHERE stopped_at IS NULL",
                            (stamp(), reason)).rowcount


def context_lines(reg: Registry, now: datetime) -> list[str]:
    rows = _open_rows(reg)
    if not rows:
        return []
    lines = ["Open live views:"]
    for r in rows:
        looked = f"last look {ago(r['last_look_at'], now)}" if r["last_look_at"] else "not looked at yet"
        tool = f"{r['app']}__{r['name']}"
        lines.append(f"  {r['app']} {r['name']} #{r['id']}, opened {ago(r['started_at'], now)}, {looked} "
                     f"(look: {tool}_look, close: {tool}_stop)")
    return lines
