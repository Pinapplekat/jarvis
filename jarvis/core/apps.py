"""Finding, validating and loading apps; the registry the loop and the CLI use."""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from jarvis.core import db
from jarvis.core.api import AppContext
from jarvis.core.manifest import ActionSpec, Manifest, ManifestError, load_manifest
from jarvis.core.schema import validate
from jarvis.core.settings import Settings, SettingsError, load_secrets

DEFAULT_APPS_DIR = Path(__file__).resolve().parent.parent / "apps"
SEP = "__"  # tool names are <app>__<action>
TMP_MAX_AGE = 600  # seconds

log = logging.getLogger("jarvis.core.apps")


@dataclass(frozen=True)
class ToolSpec:
    app: str
    action: str
    description: str
    effect: str
    input_schema: dict
    untrusted_output: bool = False

    @property
    def name(self) -> str:
        return f"{self.app}{SEP}{self.action}"


@dataclass(frozen=True)
class LoadedApp:
    manifest: Manifest
    module: ModuleType
    ctx: AppContext


def run_on_this_machine(runs_on: str) -> bool:
    """Whether this process may run an app. M2 has no node, so this Mac pretends to have every capability.

    M5 replaces this with routing to the node that has the capability.
    """
    return True


class Registry:
    def __init__(self, settings: Settings, conn: sqlite3.Connection, apps: dict[str, LoadedApp]) -> None:
        self.settings = settings
        self.conn = conn
        self.apps = apps

    def actions(self) -> list[ToolSpec]:
        return [
            ToolSpec(name, a.name, a.description, a.effect, a.input, a.untrusted_output)
            for name, app in self.apps.items()
            for a in app.manifest.actions
        ]

    def find(self, tool_name: str) -> tuple[LoadedApp, ActionSpec] | None:
        app_name, _, action_name = tool_name.partition(SEP)
        app = self.apps.get(app_name)
        if app is None:
            return None
        return next(((app, a) for a in app.manifest.actions if a.name == action_name), None)

    def close(self) -> None:
        self.conn.close()


def tmp_dir(settings: Settings) -> Path:
    path = settings.data_dir / "tmp"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def sweep_tmp(settings: Settings, now: float | None = None) -> int:
    """Delete files older than 10 minutes (leftovers from a crash). Returns how many."""
    cutoff = (time.time() if now is None else now) - TMP_MAX_AGE
    removed = 0
    for f in tmp_dir(settings).iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)
            removed += 1
    return removed


def _import_actions(apps_dir: Path, name: str) -> ModuleType:
    path = apps_dir / name / "actions.py"
    if not path.exists():
        raise ManifestError(apps_dir / name / "manifest.toml", "actions.py is missing")
    if apps_dir.resolve() == DEFAULT_APPS_DIR:
        return importlib.import_module(f"jarvis.apps.{name}.actions")
    module_name = f"falak_app_{name}_actions"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _check_handlers(manifest: Manifest, module: ModuleType) -> None:
    if manifest.context is not None and not callable(getattr(module, manifest.context, None)):
        raise ManifestError(manifest.path, f"context handler '{manifest.context}' not found in actions.py")
    for action in manifest.actions:
        for label, fn in (("handler", action.handler), ("undo handler", action.undo)):
            if fn is not None and not callable(getattr(module, fn, None)):
                raise ManifestError(manifest.path, f"action '{action.name}': {label} '{fn}' not found in actions.py")


def _app_settings(settings: Settings, manifest: Manifest) -> dict:
    section = settings.apps.get(manifest.name, {})
    if not isinstance(section, dict):
        raise SettingsError(f"[apps.{manifest.name}] must be a table")
    merged = {key: schema["default"] for key, schema in manifest.settings.items()}
    for key, value in section.items():
        if key == "enabled":
            continue
        if key not in manifest.settings:
            known = ", ".join(["enabled", *manifest.settings])
            raise SettingsError(f"[apps.{manifest.name}]: unknown setting '{key}' (known: {known})")
        problems = validate(manifest.settings[key], value, key)
        if problems:
            raise SettingsError(f"[apps.{manifest.name}] {problems[0]}")
        merged[key] = value
    return merged


def settings_for_missing_apps(settings: Settings, apps_dir: Path = DEFAULT_APPS_DIR) -> list[str]:
    """[apps.<name>] sections in settings.toml with no installed app (a typo, or a removed app)."""
    installed = {p.parent.name for p in Path(apps_dir).glob("*/manifest.toml")}
    return sorted(name for name in settings.apps if name not in installed)


def load_apps(settings: Settings, apps_dir: Path = DEFAULT_APPS_DIR) -> Registry:
    """Load every enabled app under `apps_dir`: validate, import, migrate. Raises ManifestError/SettingsError."""
    apps_dir = Path(apps_dir)
    conn = db.connect(settings)
    try:
        sweep_tmp(settings)
        for name in settings_for_missing_apps(settings, apps_dir):
            log.warning("settings.toml has [apps.%s] but no app named %s", name, name)
        secrets = load_secrets(settings.data_dir)
        loaded: dict[str, LoadedApp] = {}
        for path in sorted(apps_dir.glob("*/manifest.toml")):
            manifest = load_manifest(path)
            app_settings = _app_settings(settings, manifest)
            if settings.apps.get(manifest.name, {}).get("enabled", True) is False:
                continue
            module = _import_actions(apps_dir, manifest.name)
            _check_handlers(manifest, module)
            migrations = path.parent / "migrations"
            if migrations.is_dir():
                db.migrate(conn, migrations, app=manifest.name)
            ctx = AppContext(
                manifest.name,
                app_settings,
                {k: secrets[k] for k in manifest.secrets if k in secrets},
                conn,
                tmp_dir(settings),
            )
            loaded[manifest.name] = LoadedApp(manifest, module, ctx)
        return Registry(settings, conn, loaded)
    except BaseException:
        conn.close()
        raise
