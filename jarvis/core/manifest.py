"""App manifests: parse `manifest.toml` and enforce the contract's rules."""

from __future__ import annotations

import re
import string
import tomllib
from dataclasses import dataclass
from pathlib import Path

from jarvis.core.schema import check_schema

EFFECTS = ("read", "local", "outbound", "irreversible")
_NAME = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")  # snake_case; no "__", which joins app and action
_RUNS_ON = re.compile(r"^(host|node:[a-z][a-z0-9_]*)$")
_APP_KEYS = {"name", "description", "runs_on", "secrets"}
_ACTION_KEYS = {"name", "description", "effect", "handler", "undo", "confirm", "log_args", "input"}
_NOT_YET = {"sessions": "M5", "jobs": "M6"}


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


@dataclass(frozen=True)
class Manifest:
    name: str
    description: str
    runs_on: str
    secrets: tuple[str, ...]
    settings: dict
    actions: tuple[ActionSpec, ...]
    path: Path


def _unknown(path: Path, where: str, given: dict, allowed: set[str]) -> None:
    for key in sorted(given):
        if key in _NOT_YET and where == "the manifest":
            raise ManifestError(path, f"'{key}' is not valid yet ({_NOT_YET[key]} adds it)")
    extra = sorted(set(given) - allowed)
    if extra:
        raise ManifestError(path, f"unknown key(s) in {where}: {', '.join(extra)}")


def _text(path: Path, where: str, table: dict, key: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(path, f"{where}: '{key}' must be a non-empty string")
    return value


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
    return ActionSpec(name, description, effect, schema, handler, undo, confirm, log_args)


def load_manifest(path: Path, folder: str | None = None) -> Manifest:
    """Parse and validate one manifest. `folder` is the app's folder name (default: the file's parent)."""
    path = Path(path)
    folder = folder or path.parent.name
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ManifestError(path, str(e)) from e
    _unknown(path, "the manifest", raw, {"app", "settings", "actions"})
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
    settings = raw.get("settings", {})
    if not isinstance(settings, dict) or not all(isinstance(v, (bool, int, float, str)) for v in settings.values()):
        raise ManifestError(path, "[settings] must hold plain values (text, numbers, true/false)")
    tables = raw.get("actions", [])
    if not isinstance(tables, list) or not tables:
        raise ManifestError(path, "an app needs at least one [[actions]] entry")
    actions = tuple(_action(path, t) for t in tables)
    names = [a.name for a in actions]
    if len(set(names)) != len(names):
        raise ManifestError(path, f"duplicate action name '{next(n for n in names if names.count(n) > 1)}'")
    return Manifest(name, description, runs_on, tuple(secrets), settings, actions, path)
