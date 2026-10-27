"""Finding and importing an app's code; shared by the host (for host apps) and the node (for node apps)."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from jarvis.contract.manifest import Manifest, ManifestError

DEFAULT_APPS_DIR = Path(__file__).resolve().parent.parent / "apps"


def import_actions(apps_dir: Path, name: str) -> ModuleType:
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


def check_handlers(manifest: Manifest, module: ModuleType) -> None:
    if manifest.context is not None and not callable(getattr(module, manifest.context, None)):
        raise ManifestError(manifest.path, f"context handler '{manifest.context}' not found in actions.py")
    for key in ("start", "stop"):
        if getattr(manifest, key) is not None and not callable(getattr(module, getattr(manifest, key), None)):
            raise ManifestError(manifest.path, f"{key} handler '{getattr(manifest, key)}' not found in actions.py")
    for action in manifest.actions:
        for label, fn in (("handler", action.handler), ("undo handler", action.undo)):
            if fn is not None and not callable(getattr(module, fn, None)):
                raise ManifestError(manifest.path, f"action '{action.name}': {label} '{fn}' not found in actions.py")
    for job in manifest.jobs:
        if not callable(getattr(module, job.handler, None)):
            raise ManifestError(manifest.path, f"job '{job.name}': handler '{job.handler}' not found in actions.py")
    for session in manifest.sessions:
        for label, fn in zip(("start", "sample", "stop"), session.handlers()):
            if not callable(getattr(module, fn, None)):
                raise ManifestError(manifest.path, f"session '{session.name}': {label} handler '{fn}' not found in actions.py")


def manifest_paths(*dirs: Path) -> list[Path]:
    return sorted(p for d in dirs for p in Path(d).glob("*/manifest.toml"))
