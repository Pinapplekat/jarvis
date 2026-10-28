"""The desk on the node: starts Falak Ears and the listening thread when the host enables it, stops them with the node."""

from __future__ import annotations

import logging
import queue
import threading
import time

from jarvis.contract.api import AppContext, Result

log = logging.getLogger("jarvis.apps.desk")

QUEUE_MAX = 400  # about 12 seconds of microphone frames; past that the session is stuck and frames are dropped
TICK = 0.25


class Runner:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.inbox: queue.Queue = queue.Queue(QUEUE_MAX)
        self.session = None
        self._helper = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="desk", daemon=True)
        self._dropped = False

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.inbox.put(None)
        self._thread.join(5)
        if self._helper is not None:
            self._helper.close()

    def post(self, kind: str, payload: object = None) -> None:
        try:
            self.inbox.put_nowait((kind, payload))
        except queue.Full:
            if not self._dropped:
                self._dropped = True
                log.warning("the desk session is behind; dropping frames")

    def _setup(self):
        """Build the helper, find the models, start the helper app. Imports the `desk` extra's packages here."""
        from jarvis.apps.desk import build, helper, wake
        from jarvis.apps.desk.machine import Timing
        from jarvis.apps.desk.session import DeskSession

        s = self.ctx.settings
        desk_dir = self.ctx.data_dir
        wake.download_models(desk_dir) if not (wake.models_dir(desk_dir) / "silero_vad.onnx").exists() else None
        build.build(desk_dir, str(s["signing_identity"]))
        link = helper.launch(build.app_path(desk_dir), desk_dir / "run",
                             lambda pcm: self.post("mic", pcm), lambda ev: self.post("helper", ev))
        self._helper = link
        link.command({"cmd": "hotkey", "key": s["push_to_talk"]})
        timing = Timing(end_silence_ms=int(s["end_silence_ms"]), barge_in_ms=int(s["barge_in_ms"]),
                        follow_up_s=float(s["follow_up_seconds"]))
        return DeskSession(helper=link, vad=wake.load_vad(desk_dir),
                           wake=wake.load_wake(desk_dir, time.monotonic) if s["wake_word"] else None,
                           send=self.ctx.send_to_host, timing=timing, chimes=bool(s["chimes"]),
                           idle_seconds=wake.idle_seconds, on_error=self._helper_error)

    def _helper_error(self, what: str) -> None:
        if "not allowed" in what:
            self.ctx.notify("The desk can't hear: Falak Ears needs the Microphone permission. Allow it in System "
                            "Settings > Privacy & Security > Microphone.")

    def _run(self) -> None:
        try:
            self.session = self._setup()
        except ImportError as e:  # the `desk` extra isn't installed on this node: the desk is simply off
            log.warning("the desk is off: %s (install it with `uv sync --extra desk`)", e)
            return
        except Exception as e:
            log.exception("the desk could not start")
            self.ctx.notify(f"The desk couldn't start: {e}")
            return
        self.ctx.on_host_frame(lambda frame: self.post("host", frame))
        threading.Thread(target=self._ticks, name="desk-tick", daemon=True).start()
        while (item := self.inbox.get()) is not None and not self._stop.is_set():
            try:
                self.session.handle(*item)
            except Exception:
                log.exception("the desk session failed on %s", item[0])

    def _ticks(self) -> None:
        while not self._stop.wait(TICK):
            self.post("tick")


_RUNNER: Runner | None = None


def start(ctx: AppContext) -> None:
    global _RUNNER
    _RUNNER = Runner(ctx)
    _RUNNER.start()


def stop(ctx: AppContext) -> None:
    if _RUNNER is not None:
        _RUNNER.stop()


def status(ctx: AppContext) -> Result:
    session = _RUNNER.session if _RUNNER else None
    if session is None:
        return Result("The desk isn't running.", data={"state": "off"})
    return Result(f"The desk is {session.state}.", data={"state": session.state})
