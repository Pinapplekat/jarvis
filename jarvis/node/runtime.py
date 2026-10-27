"""Running device apps for the host: load the node apps, execute one call, keep the live resources honest."""

from __future__ import annotations

import base64
import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from jarvis.contract.api import ActionError, AppContext, Result, SessionStarted
from jarvis.contract.envfile import load_secrets
from jarvis.contract.loader import DEFAULT_APPS_DIR, check_handlers, import_actions, manifest_paths
from jarvis.contract.manifest import Manifest, ManifestError, load_manifest
from jarvis.protocol import messages as m

log = logging.getLogger("jarvis.node.runtime")


@dataclass
class _App:
    manifest: Manifest
    module: ModuleType
    lock: threading.Lock  # one handler of an app at a time
    ctx: AppContext | None = None  # set by configure() once the host has sent the settings


class NodeRuntime:
    def __init__(self, node_dir: Path, tmp_dir: Path, apps_dir: Path = DEFAULT_APPS_DIR,
                 extra_dirs: tuple[Path, ...] = ()) -> None:
        self._tmp, self._secrets = tmp_dir, load_secrets(node_dir)
        self._apps: dict[str, _App] = {}
        self._live: list[tuple[str, str, dict]] = []  # (app, stop handler, state) of sessions started here
        self._live_lock = threading.Lock()
        for path in manifest_paths(apps_dir, *extra_dirs):
            try:
                manifest = load_manifest(path)
                if not manifest.runs_on.startswith("node:"):
                    continue
                module = import_actions(path.parent.parent, manifest.name)
                check_handlers(manifest, module)
            except (ManifestError, ImportError) as e:
                log.error("skipping %s: %s", path.parent.name, e)
                continue
            self._apps[manifest.name] = _App(manifest, module, threading.Lock())

    @property
    def apps(self) -> list[str]:
        return sorted(self._apps)

    @property
    def capabilities(self) -> list[str]:
        return sorted({a.manifest.runs_on.split(":", 1)[1] for a in self._apps.values()})

    def configure(self, settings: dict[str, dict]) -> None:
        """Take the host's merged settings (only for the apps it enabled); other apps refuse calls."""
        for name, app in self._apps.items():
            if name in settings:
                secrets = {k: self._secrets[k] for k in app.manifest.secrets if k in self._secrets}
                app.ctx = AppContext(name, settings[name], secrets, None, self._tmp)
            else:
                app.ctx = None

    # ---- one call --------------------------------------------------------------------------------------

    def execute(self, call: m.Call, abandoned: threading.Event | None = None) -> m.CallResult:
        """Run a handler (blocking; the client calls this in a worker thread). Never raises."""
        app = self._apps.get(call.app)
        if app is None or app.ctx is None:
            return m.CallResult(call.id, False, error=f"{call.app} isn't enabled on this device.")
        paths: list[Path] = []
        try:
            with app.lock:
                out = self._invoke(app, call)
                if abandoned is not None and abandoned.is_set():  # the host stopped waiting
                    self._discard(app, call, out)
                    return m.CallResult(call.id, False, error="The call timed out.")
                return self._package(call, out, paths)
        except ActionError as e:
            return m.CallResult(call.id, False, error=str(e))
        except Exception:
            log.exception("%s.%s crashed", call.app, call.handler)
            return m.CallResult(call.id, False, crash=True)
        finally:
            for p in paths:
                p.unlink(missing_ok=True)

    def _invoke(self, app: _App, call: m.Call) -> Result | SessionStarted:
        fn = getattr(app.module, call.handler, None)
        if not callable(fn) or call.handler not in self._known_handlers(app.manifest):
            raise ActionError(f"{call.app} has no handler called {call.handler}.")
        out = fn(app.ctx, call.args) if call.as_arg else fn(app.ctx, **call.args)
        if isinstance(out, SessionStarted):
            spec = next(s for s in app.manifest.sessions if s.start == call.handler)
            with self._live_lock:
                self._live.append((call.app, spec.stop, out.state))
        elif isinstance(out, Result):
            with self._live_lock:
                self._live = [x for x in self._live if not (x[0] == call.app and x[1] == call.handler and x[2] == call.args)]
        else:
            raise TypeError(f"handler returned {type(out).__name__}")
        return out

    @staticmethod
    def _known_handlers(manifest: Manifest) -> set[str]:
        names = {t.handler for t in manifest.tools} | {t.undo for t in manifest.actions if t.undo}
        return names

    def _package(self, call: m.Call, out: Result | SessionStarted, paths: list[Path]) -> m.CallResult:
        if isinstance(out, SessionStarted):
            return m.CallResult(call.id, True, out.text, {"state": out.state})
        attachments, total = [], 0
        tmp = self._tmp.resolve()
        for att in out.attachments:
            paths.append(att.path)
            if att.path.resolve().parent != tmp:
                raise ValueError(f"attachment {att.path} is outside the temp folder")
            raw = att.path.read_bytes()
            total += len(raw)
            attachments.append({"kind": att.kind, "mime": att.mime, "data": base64.b64encode(raw).decode()})
        if total > m.MAX_ATTACHMENT_BYTES:
            raise ActionError("The picture was too big to send (over 10 MB).")
        result = m.CallResult(call.id, True, out.text, out.data, out.undo, out.subject, attachments)
        json.loads(result.to_json())  # a result that can't be sent is a crash here, not a dropped frame later
        return result

    def _discard(self, app: _App, call: m.Call, out: Result | SessionStarted) -> None:
        """A call finished after the host gave up on it: delete its files and close any view it opened."""
        if isinstance(out, Result):
            for att in out.attachments:
                att.path.unlink(missing_ok=True)
        else:
            self._close(app.manifest.name, next(s.stop for s in app.manifest.sessions if s.start == call.handler), out.state)

    # ---- live resources --------------------------------------------------------------------------------

    def _close(self, app_name: str, stop: str, state: dict) -> None:
        app = self._apps[app_name]
        with self._live_lock:
            self._live = [x for x in self._live if not (x[0] == app_name and x[2] == state)]
        try:
            out = getattr(app.module, stop)(app.ctx, state)
            for att in getattr(out, "attachments", ()):
                att.path.unlink(missing_ok=True)
        except Exception:
            log.exception("closing a live %s resource failed", app_name)

    def close_live(self) -> int:
        """The host link dropped, so nobody can stop these sessions any more: stop them here."""
        with self._live_lock:
            live = list(self._live)
        for app_name, stop, state in live:
            self._close(app_name, stop, state)
        return len(live)
