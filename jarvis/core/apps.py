"""Finding, validating and loading apps; the registry the loop and the CLI use."""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from jarvis.core import db
from jarvis.contract.api import AppContext
from jarvis.contract.loader import DEFAULT_APPS_DIR, check_handlers, import_actions, manifest_paths
from jarvis.contract.manifest import ActionSpec, Manifest, ManifestError, load_manifest
from jarvis.contract.schema import validate
from jarvis.contract.envfile import load_secrets
from jarvis.core.settings import Settings, SettingsError

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
    module: ModuleType | None  # None for apps that run on a node: the host never imports their code
    ctx: AppContext


class Registry:
    def __init__(self, settings: Settings, conn: sqlite3.Connection, apps: dict[str, LoadedApp]) -> None:
        self.settings = settings
        self.conn = conn
        self.apps = apps
        self.nodes = None  # the NodeLink that reaches devices; set by jarvis serve (see core/dispatch.py)

    def actions(self) -> list[ToolSpec]:
        return [
            ToolSpec(name, a.name, a.description, a.effect, a.input, a.untrusted_output)
            for name, app in self.apps.items()
            for a in app.manifest.tools
        ]

    def find(self, tool_name: str) -> tuple[LoadedApp, ActionSpec] | None:
        app_name, _, action_name = tool_name.partition(SEP)
        app = self.apps.get(app_name)
        if app is None:
            return None
        return next(((app, a) for a in app.manifest.tools if a.name == action_name), None)

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


def node_app_settings(reg: Registry) -> dict[str, dict]:
    """Merged settings of every enabled app that runs on a node; the welcome message hands them to the node."""
    return {name: dict(app.ctx.settings) for name, app in reg.apps.items() if app.manifest.runs_on != "host"}


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


def load_apps(settings: Settings, apps_dir: Path = DEFAULT_APPS_DIR, extra_dirs: tuple[Path, ...] = ()) -> Registry:
    """Load every enabled app under `apps_dir` (and `extra_dirs`): validate, import, migrate.

    Raises ManifestError/SettingsError.
    """
    apps_dir = Path(apps_dir)
    conn = db.connect(settings)
    try:
        sweep_tmp(settings)
        for name in settings_for_missing_apps(settings, apps_dir):
            log.warning("settings.toml has [apps.%s] but no app named %s", name, name)
        secrets = load_secrets(settings.data_dir)
        loaded: dict[str, LoadedApp] = {}
        for path in manifest_paths(apps_dir, *extra_dirs):
            manifest = load_manifest(path)
            app_settings = _app_settings(settings, manifest)
            if settings.apps.get(manifest.name, {}).get("enabled", True) is False:
                continue
            on_host = manifest.runs_on == "host"
            module = import_actions(path.parent.parent, manifest.name) if on_host else None
            if module is not None:
                check_handlers(manifest, module)
            migrations = path.parent / "migrations"
            if on_host and migrations.is_dir():
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
