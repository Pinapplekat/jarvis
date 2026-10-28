"""Running device apps for the host: load the node apps, execute one call, keep the live resources honest."""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
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
        self._tmp, self._secrets, self._node_dir = tmp_dir, load_secrets(node_dir), node_dir
        self._outbox = node_dir / "outbox"  # events not yet acked by the host, one json file each
        self._outbox_lock = threading.Lock()
        self.on_event: Callable[[], None] = lambda: None  # set by the client: "there is something to send"
        self.send_frame: Callable[[str | bytes], None] = lambda frame: None  # set by the client: a frame to the host
        self._started: set[str] = set()
        self._clear_tmp()
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

    def _clear_tmp(self) -> None:
        """Nothing is live before the runtime exists, so anything in tmp is left over from a crash or a kill:
        photos and frames of Elijah's camera and screen. They go."""
        if not self._tmp.is_dir():
            return
        removed = 0
        for f in self._tmp.iterdir():
            if f.is_file():
                f.unlink(missing_ok=True)
                removed += 1
        if removed:
            log.info("removed %d leftover file(s) from %s", removed, self._tmp)

    @property
    def apps(self) -> list[str]:
        return sorted(self._apps)

    @property
    def capabilities(self) -> list[str]:
        return sorted({a.manifest.runs_on.split(":", 1)[1] for a in self._apps.values()})

    def configure(self, settings: dict[str, dict]) -> None:
        """Take the host's merged settings (only for the apps it enabled); other apps refuse calls. An app's `start`
        handler runs once, the first time it is enabled."""
        for name, app in self._apps.items():
            if name in settings:
                secrets = {k: self._secrets[k] for k in app.manifest.secrets if k in self._secrets}
                if app.ctx is not None:  # a reconnect: same context (a started app holds it), fresh settings
                    app.ctx.settings, app.ctx.secrets = settings[name], secrets
                else:
                    app.ctx = AppContext(name, settings[name], secrets, None, self._tmp,
                                         data_dir=self._node_dir / name)
                    app.ctx.notify_hook = self.notify
                    app.ctx.link_hook = lambda frame: self.send_frame(frame)
                if app.manifest.start and name not in self._started:
                    self._started.add(name)
                    getattr(app.module, app.manifest.start)(app.ctx)
            else:
                app.ctx = None

    def close(self) -> None:
        """The node is shutting down: let each started app stop its background threads."""
        for name in sorted(self._started):
            app = self._apps[name]
            if app.manifest.stop and app.ctx is not None:
                try:
                    getattr(app.module, app.manifest.stop)(app.ctx)
                except Exception:
                    log.exception("stopping %s failed", name)
        self._started.clear()

    def host_frame(self, frame: str | bytes) -> None:
        """A voice frame from the host: every started app that listens gets it (only the desk does)."""
        for app in self._apps.values():
            listener = app.ctx._frame_listener if app.ctx is not None else None
            if listener is not None:
                try:
                    listener(frame)
                except Exception:
                    log.exception("%s failed to take a frame from the host", app.manifest.name)

    # ---- events: what apps say first ------------------------------------------------------------------

    def notify(self, app: str, text: str) -> None:
        """ctx.notify on a node: the message is on disk before anything is sent, and stays there until the host
        acks it, so a dropped link or a restart loses nothing."""
        event = m.Event(uuid.uuid4().hex, app, text[: m.MAX_EVENT_TEXT])
        with self._outbox_lock:
            self._outbox.mkdir(mode=0o700, parents=True, exist_ok=True)
            path = self._outbox / f"{event.id}.json"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"at": time.time_ns(), "app": event.app, "text": event.text}))
            os.replace(tmp, path)
        self.on_event()

    def unacked(self) -> list[m.Event]:
        """Every event the host hasn't acked, oldest first."""
        found = []
        with self._outbox_lock:
            for path in self._outbox.glob("*.json") if self._outbox.is_dir() else ():
                try:
                    body = json.loads(path.read_text())
                    found.append((body["at"], m.Event(path.stem, str(body["app"]), str(body["text"]))))
                except (OSError, ValueError, KeyError):
                    log.warning("skipping unreadable event file %s", path.name)
        return [e for _, e in sorted(found, key=lambda x: (x[0], x[1].id))]

    def ack(self, event_id: str) -> None:
        if event_id.isalnum():  # an id is a hex uuid; never a path
            with self._outbox_lock:
                (self._outbox / f"{event_id}.json").unlink(missing_ok=True)

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
