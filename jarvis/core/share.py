"""ctx.call: one app running another app's shared read action, through the same dispatch as everything else."""

from __future__ import annotations

import sqlite3

from jarvis.contract.api import AppUnavailable, Result
from jarvis.contract.schema import fill_defaults, validate
from jarvis.core import dispatch
from jarvis.core.apps import Registry


def install(reg: Registry) -> None:
    """Give every host app's context a working ctx.call."""

    def hook(caller: str, app: str, action: str, args: dict, conn: sqlite3.Connection | None) -> Result:
        return call(reg, caller, app, action, args, conn)

    for loaded in reg.apps.values():
        loaded.ctx.call_hook = hook


def call(reg: Registry, caller: str, app_name: str, action_name: str, args: dict,
         conn: sqlite3.Connection | None = None) -> Result:
    """Run `app_name`'s shared action for `caller`. A call the manifests don't allow is a bug and raises
    PermissionError; a missing app or device raises AppUnavailable. Not written to the action log: neither
    Elijah nor the model did it."""
    me = reg.apps.get(caller)
    if me is None or app_name not in me.manifest.uses:
        raise PermissionError(f"{caller} calls {app_name}, but its manifest doesn't list it in uses")
    target = reg.apps.get(app_name)
    if target is None:
        raise AppUnavailable(f"{app_name} isn't installed or is turned off.")
    spec = next((a for a in target.manifest.actions if a.name == action_name), None)
    if spec is None or not spec.share:
        raise PermissionError(f"{app_name}.{action_name} isn't a shared action")
    args = fill_defaults(spec.input, args)
    problems = validate(spec.input, args)
    if problems:
        raise ValueError(f"{caller} called {app_name}.{action_name} with bad input: {'; '.join(problems)}")
    me.ctx.log.debug("calling %s.%s", app_name, action_name)
    if target.manifest.runs_on == "host":
        ctx = target.ctx.with_db(conn) if conn is not None else target.ctx
        result = getattr(target.module, spec.handler)(ctx, **args)
        if not isinstance(result, Result):
            raise TypeError(f"{app_name}.{action_name} returned {type(result).__name__}, not Result")
    else:
        try:
            result, _ = dispatch.call(reg, target, spec.handler, args, spec.timeout_s)
        except dispatch.NodeError as e:
            raise AppUnavailable(str(e)) from None
    for att in result.attachments:  # another app gets the text and data, never files to clean up
        att.path.unlink(missing_ok=True)
    return result
