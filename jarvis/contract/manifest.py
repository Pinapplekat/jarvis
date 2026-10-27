"""App manifests: parse `manifest.toml` and enforce the contract's rules."""

from __future__ import annotations

import re
import string
import tomllib
from dataclasses import dataclass
from pathlib import Path

from jarvis.contract.schema import check_schema, validate
from jarvis.contract.timespec import MIN_EVERY_S, parse_clock, parse_interval

EFFECTS = ("read", "local", "outbound", "irreversible")
_NAME = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")  # snake_case; no "__", which joins app and action
_RUNS_ON = re.compile(r"^(host|node:[a-z][a-z0-9_]*)$")
_APP_KEYS = {"name", "description", "runs_on", "secrets", "context", "uses"}
_ACTION_KEYS = {"name", "description", "effect", "handler", "undo", "confirm", "log_args", "untrusted_output", "input",
                "timeout_s", "share", "tool", "sends_data"}
_SESSION_KEYS = {"name", "description", "effect", "untrusted_output", "idle_minutes", "start", "sample", "stop", "input",
                 "timeout_s"}
_JOB_KEYS = {"name", "title", "description", "handler", "kind", "every", "at", "quiet", "needs", "timeout_s"}
JOB_KINDS = ("deliver", "sync", "think")
JOB_QUIET = ("ignore", "hold", "skip")
JOB_NEEDS = ("network",)
DEFAULT_TIMEOUT_S = 30
PHASES = ("start", "look", "stop")  # the three tools a session gives the model


class ManifestError(Exception):
    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


@dataclass(frozen=True)
class ActionSpec:
    name: str
    description: str
    effect: str
    input: dict
    handler: str
    undo: str | None = None
    confirm: str | None = None
    log_args: bool = True
    untrusted_output: bool = False  # the result can carry someone else's words (a photo, a page, an email)
    timeout_s: int = DEFAULT_TIMEOUT_S  # how long a device may take before the call fails
    session: str | None = None  # set on the tools a [[sessions]] entry generates
    phase: str | None = None  # "start", "look" or "stop"
    share: bool = False  # other apps that list this one in `uses` may call it (read actions only)
    tool: bool = True  # offered to the model; false for actions only other apps call
    sends_data: bool = False  # its arguments leave the device (a search query, a URL)


@dataclass(frozen=True)
class SessionSpec:
    name: str
    description: str
    effect: str
    input: dict
    start: str  # handler(ctx, **input) -> SessionStarted
    sample: str  # handler(ctx, state) -> Result
    stop: str  # handler(ctx, state) -> Result
    idle_minutes: int = 15
    untrusted_output: bool = False
    timeout_s: int = DEFAULT_TIMEOUT_S

    def handlers(self) -> tuple[str, str, str]:
        return self.start, self.sample, self.stop

    def tools(self) -> tuple[ActionSpec, ...]:
        """The three tools the core gives the model: <name>_start, <name>_look, <name>_stop."""
        ident = {"type": "object", "required": ["session_id"], "additionalProperties": False,
                 "properties": {"session_id": {"type": "integer", "description": "The number of the open session."}}}
        common = dict(effect=self.effect, untrusted_output=self.untrusted_output, timeout_s=self.timeout_s,
                      session=self.name)
        return (
            ActionSpec(f"{self.name}_start", self.description + " Returns the session number and the first look.",
                       input=self.input, handler=self.start, phase="start", **common),
            ActionSpec(f"{self.name}_look", f"Look again at the open {self.name} session, as often as you need.",
                       input=ident, handler=self.sample, phase="look", **common),
            ActionSpec(f"{self.name}_stop", f"Close the open {self.name} session when it's no longer needed.",
                       input=ident, handler=self.stop, phase="stop", **common),
        )


@dataclass(frozen=True)
class JobSpec:
    name: str
    title: str  # how failure messages name it: "Canvas sync keeps failing"
    description: str
    handler: str  # handler(ctx, run: JobRun) -> JobResult
    kind: str  # deliver | sync | think
    every: int | None  # seconds
    at: tuple[str, ...]  # "HH:MM", or "$<setting>" naming a string setting of the app that holds one
    quiet: str  # ignore | hold | skip
    needs: tuple[str, ...]
    timeout_s: float


@dataclass(frozen=True)
class Manifest:
    name: str
    description: str
    runs_on: str
    secrets: tuple[str, ...]
    settings: dict  # setting name -> schema (type, default, bounds, description)
    actions: tuple[ActionSpec, ...]
    path: Path
    context: str | None = None  # handler returning a short text block for every turn prompt
    sessions: tuple[SessionSpec, ...] = ()
    jobs: tuple[JobSpec, ...] = ()
    uses: tuple[str, ...] = ()  # apps whose shared actions this one may call with ctx.call

    @property
    def tools(self) -> tuple[ActionSpec, ...]:
        """Every action the app has: its actions plus the three generated for each session."""
        return self.actions + tuple(t for s in self.sessions for t in s.tools())

    @property
    def model_tools(self) -> tuple[ActionSpec, ...]:
        """The tools the model is offered: everything but actions marked tool = false."""
        return tuple(t for t in self.tools if t.tool)


def _unknown(path: Path, where: str, given: dict, allowed: set[str]) -> None:
    extra = sorted(set(given) - allowed)
    if extra:
        raise ManifestError(path, f"unknown key(s) in {where}: {', '.join(extra)}")


def _text(path: Path, where: str, table: dict, key: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(path, f"{where}: '{key}' must be a non-empty string")
    return value


def _timeout(path: Path, where: str, table: dict) -> int:
    value = table.get("timeout_s", DEFAULT_TIMEOUT_S)
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 300:
        raise ManifestError(path, f"{where}: timeout_s must be a whole number from 1 to 300")
    return value


def _session(path: Path, table: dict) -> SessionSpec:
    _unknown(path, "a session", table, _SESSION_KEYS)
    name = _text(path, "a session", table, "name")
    where = f"session '{name}'"
    if not _NAME.match(name):
        raise ManifestError(path, f"{where}: name must be snake_case")
    description = _text(path, where, table, "description")
    effect = table.get("effect")
    if effect not in ("read", "local"):
        raise ManifestError(path, f"{where}: effect must be read or local (a session can't ask first)")
    handlers = []
    for key in ("start", "sample", "stop"):
        fn = table.get(key)
        if not (isinstance(fn, str) and fn.isidentifier()):
            raise ManifestError(path, f"{where}: '{key}' must be a handler function name")
        handlers.append(fn)
    idle = table.get("idle_minutes", 15)
    if not isinstance(idle, int) or isinstance(idle, bool) or not 1 <= idle <= 120:
        raise ManifestError(path, f"{where}: idle_minutes must be a whole number from 1 to 120")
    untrusted = table.get("untrusted_output", False)
    if not isinstance(untrusted, bool):
        raise ManifestError(path, f"{where}: untrusted_output must be true or false")
    schema = table.get("input", {"type": "object", "additionalProperties": False})
    problems = check_schema(schema)
    if not problems and schema.get("type") != "object":
        problems = ["input: must have type object"]
    if problems:
        raise ManifestError(path, f"{where}: {problems[0]}")
    return SessionSpec(name, description, effect, schema, *handlers, idle, untrusted, _timeout(path, where, table))


def _action(path: Path, table: dict) -> ActionSpec:
    _unknown(path, "an action", table, _ACTION_KEYS)
    name = _text(path, "an action", table, "name")
    where = f"action '{name}'"
    if not _NAME.match(name):
        raise ManifestError(path, f"{where}: name must be snake_case")
    description = _text(path, where, table, "description")
    effect = table.get("effect")
    if effect not in EFFECTS:
        raise ManifestError(path, f"{where}: effect must be one of {', '.join(EFFECTS)}")
    handler = table.get("handler", name)
    undo, confirm = table.get("undo"), table.get("confirm")
    for label, fn in (("handler", handler), ("undo", undo)):
        if fn is not None and not (isinstance(fn, str) and fn.isidentifier()):
            raise ManifestError(path, f"{where}: {label} must be a function name")
    if undo is not None and effect != "local":
        raise ManifestError(path, f"{where}: 'undo' is only allowed on local actions")
    log_args = table.get("log_args", True)
    if not isinstance(log_args, bool):
        raise ManifestError(path, f"{where}: log_args must be true or false")
    schema = table.get("input", {"type": "object", "additionalProperties": False})
    problems = check_schema(schema)
    if not problems and schema.get("type") != "object":
        problems = ["input: must have type object"]
    if problems:
        raise ManifestError(path, f"{where}: {problems[0]}")
    if effect in ("outbound", "irreversible"):
        if not isinstance(confirm, str) or not confirm.strip():
            raise ManifestError(path, f"{where}: {effect} actions need a 'confirm' question")
    elif confirm is not None:
        raise ManifestError(path, f"{where}: 'confirm' is only for outbound and irreversible actions")
    if confirm is not None:
        for _, field, _, _ in string.Formatter().parse(confirm):
            if field is not None and field not in schema.get("properties", {}):
                raise ManifestError(path, f"{where}: confirm uses {{{field}}}, which is not an input property")
    untrusted = table.get("untrusted_output", False)
    if not isinstance(untrusted, bool):
        raise ManifestError(path, f"{where}: untrusted_output must be true or false")
    flags = {}
    for key, default in (("share", False), ("tool", True), ("sends_data", False)):
        flags[key] = table.get(key, default)
        if not isinstance(flags[key], bool):
            raise ManifestError(path, f"{where}: {key} must be true or false")
    if flags["share"] and effect != "read":
        raise ManifestError(path, f"{where}: only read actions can be shared with other apps")
    if not flags["tool"] and not flags["share"]:
        raise ManifestError(path, f"{where}: an action with tool = false must be shared, or nothing can call it")
    return ActionSpec(name, description, effect, schema, handler, undo, confirm, log_args, untrusted,
                      _timeout(path, where, table), **flags)


def _job(path: Path, table: dict, settings: dict) -> JobSpec:
    _unknown(path, "a job", table, _JOB_KEYS)
    name = _text(path, "a job", table, "name")
    where = f"job '{name}'"
    if not _NAME.match(name):
        raise ManifestError(path, f"{where}: name must be snake_case")
    description = _text(path, where, table, "description")
    title = table.get("title", name.replace("_", " "))
    if not isinstance(title, str) or not title.strip():
        raise ManifestError(path, f"{where}: title must be text")
    handler = table.get("handler", name)
    if not (isinstance(handler, str) and handler.isidentifier()):
        raise ManifestError(path, f"{where}: handler must be a function name")
    kind, quiet = table.get("kind"), table.get("quiet", "hold")
    if kind not in JOB_KINDS:
        raise ManifestError(path, f"{where}: kind must be one of {', '.join(JOB_KINDS)}")
    if quiet not in JOB_QUIET:
        raise ManifestError(path, f"{where}: quiet must be one of {', '.join(JOB_QUIET)}")
    if kind == "think" and quiet == "ignore":
        raise ManifestError(path, f"{where}: a think job may not use quiet = \"ignore\" (use hold or skip)")
    needs = table.get("needs", [])
    if not isinstance(needs, list) or any(n not in JOB_NEEDS for n in needs):
        raise ManifestError(path, f"{where}: needs takes only: {', '.join(JOB_NEEDS)}")
    every, at = table.get("every"), table.get("at", [])
    if every is None and not at:
        raise ManifestError(path, f"{where}: needs 'every' or 'at' (or both)")
    if every is not None and parse_interval(every) is None:
        raise ManifestError(path, f"{where}: every must look like 30s, 15m or 6h, and be at least {MIN_EVERY_S}s")
    times = [at] if isinstance(at, str) else at
    if not isinstance(times, list) or not all(isinstance(t, str) for t in times):
        raise ManifestError(path, f"{where}: at must be a time or a list of times like \"07:00\"")
    for t in times:
        if t.startswith("$"):
            if settings.get(t[1:], {}).get("type") != "string":
                raise ManifestError(path, f"{where}: at = \"{t}\" must name a string setting of the app")
        elif parse_clock(t) is None:
            raise ManifestError(path, f"{where}: at time '{t}' must look like 07:00")
    timeout = table.get("timeout_s", 60)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 600:
        raise ManifestError(path, f"{where}: timeout_s must be a whole number from 1 to 600")
    return JobSpec(name, title, description, handler, kind, parse_interval(every) if every else None, tuple(times),
                   quiet, tuple(needs), timeout)


SETTING_TYPES = ("string", "integer", "number", "boolean")


def _settings(path: Path, raw: object) -> dict:
    if not isinstance(raw, dict):
        raise ManifestError(path, "[settings] must be a table of [settings.<name>] declarations")
    for key, schema in raw.items():
        where = f"setting '{key}'"
        problems = check_schema(schema, key) if isinstance(schema, dict) else [f"{key}: must be a table"]
        if problems:
            raise ManifestError(path, f"{where}: {problems[0]}")
        if schema["type"] not in SETTING_TYPES:
            raise ManifestError(path, f"{where}: type must be one of {', '.join(SETTING_TYPES)}")
        if "default" not in schema:
            raise ManifestError(path, f"{where}: needs a default")
        problems = validate(schema, schema["default"], key)
        if problems:
            raise ManifestError(path, f"{where}: default is invalid ({problems[0]})")
    return raw


def load_manifest(path: Path, folder: str | None = None) -> Manifest:
    """Parse and validate one manifest. `folder` is the app's folder name (default: the file's parent)."""
    path = Path(path)
    folder = folder or path.parent.name
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ManifestError(path, str(e)) from e
    _unknown(path, "the manifest", raw, {"app", "settings", "actions", "sessions", "jobs"})
    app = raw.get("app")
    if not isinstance(app, dict):
        raise ManifestError(path, "missing [app] table")
    _unknown(path, "[app]", app, _APP_KEYS)
    name = _text(path, "[app]", app, "name")
    if name != folder:
        raise ManifestError(path, f"app name '{name}' must equal the folder name '{folder}'")
    if not _NAME.match(name):
        raise ManifestError(path, "app name must be snake_case")
    description = _text(path, "[app]", app, "description")
    runs_on = app.get("runs_on", "host")
    if not isinstance(runs_on, str) or not _RUNS_ON.match(runs_on):
        raise ManifestError(path, "runs_on must be 'host' or 'node:<capability>'")
    secrets = app.get("secrets", [])
    if not isinstance(secrets, list) or not all(isinstance(s, str) for s in secrets):
        raise ManifestError(path, "secrets must be a list of .env key names")
    settings = _settings(path, raw.get("settings", {}))
    context = app.get("context")
    if context is not None and not (isinstance(context, str) and context.isidentifier()):
        raise ManifestError(path, "context must be a function name")
    uses = app.get("uses", [])
    if not isinstance(uses, list) or not all(isinstance(u, str) and _NAME.match(u) for u in uses):
        raise ManifestError(path, "uses must be a list of app names")
    if name in uses:
        raise ManifestError(path, "an app doesn't list itself in uses")
    tables, session_tables, job_tables = raw.get("actions", []), raw.get("sessions", []), raw.get("jobs", [])
    if not all(isinstance(t, list) for t in (tables, session_tables, job_tables)):
        raise ManifestError(path, "[[actions]], [[sessions]] and [[jobs]] must be lists of tables")
    if not tables and not session_tables and not job_tables:
        raise ManifestError(path, "an app needs at least one [[actions]], [[sessions]] or [[jobs]] entry")
    if job_tables and runs_on != "host":
        raise ManifestError(path, "[[jobs]] only run for host apps for now (this app runs on a node)")
    actions = tuple(_action(path, t) for t in tables)
    sessions = tuple(_session(path, t) for t in session_tables)
    jobs = tuple(_job(path, t, settings) for t in job_tables)
    job_names = [j.name for j in jobs]
    if len(set(job_names)) != len(job_names):
        raise ManifestError(path, f"duplicate job name '{next(n for n in job_names if job_names.count(n) > 1)}'")
    names = [a.name for a in actions + tuple(t for s in sessions for t in s.tools())]
    if len(set(names)) != len(names):
        raise ManifestError(path, f"duplicate tool name '{next(n for n in names if names.count(n) > 1)}'")
    return Manifest(name, description, runs_on, tuple(secrets), settings, actions, path, context, sessions, jobs,
                    tuple(uses))
