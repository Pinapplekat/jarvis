"""Background jobs: commands that keep running after the call that started them.

A job is launched detached in its own session, with its output in `<terminal data>/jobs/<id>/job.log`. A tiny
wrapper process waits for it and writes the exit code, so a later look can tell whether it finished and how. A watcher
thread files one message when a job ends and one if it goes quiet for `job_stall_minutes`; it sleeps while no job
is running. Ported from legacy/jarvis/jobs.py.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from jarvis.apps.terminal import sandbox
from jarvis.apps.terminal.run import command_text

log = logging.getLogger("jarvis.apps.terminal")
TAIL_LINES, TAIL_CHARS = 20, 3000
KEEP_DAYS = 14  # finished job folders older than this are removed
CHECK_EVERY = 15  # seconds, while a job is running
WANTED_INPUT = re.compile(r"EOFError: EOF when reading a line|Inappropriate ioctl for device|"
                          r"stdin is not a tty|not a terminal|Press (?:any key|enter)", re.I)

# Runs the command, then writes its exit code. Plain argv, no shell; started with -I so nothing in the job's folder
# can be imported in its place.
WRAPPER = """import os, signal, subprocess, sys
d, argv = sys.argv[1], sys.argv[2:]
signal.signal(signal.SIGTERM, lambda *a: None)  # a kill reaches the command; the wrapper stays to record it
with open(os.path.join(d, "job.log"), "ab", buffering=0) as log:
    try:
        code = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT).wait()
    except OSError as e:
        log.write(("couldn't run it: %s\\n" % e).encode())
        code = 127
with open(os.path.join(d, "exit.tmp"), "w") as f:
    f.write(str(code))
os.replace(os.path.join(d, "exit.tmp"), os.path.join(d, "exit"))
"""


def duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"


def describe_state(s: dict) -> str:
    if s["state"] == "running":
        return f"running {duration(s['runtime'])}"
    if s["state"] == "lost":
        return f"stopped without an exit code after {duration(s['runtime'])}"
    code = s["code"]
    how = "finished OK" if code == 0 else f"killed by signal {-code}" if code < 0 else f"failed (exit {code})"
    return f"{how} after {duration(s['runtime'])}"


class Jobs:
    def __init__(self, folder: Path) -> None:
        self.dir = folder

    def _dir(self, job_id: int) -> Path:
        return self.dir / str(int(job_id))

    def _meta(self, job_id: int) -> dict | None:
        try:
            return json.loads((self._dir(job_id) / "meta.json").read_text())
        except (OSError, ValueError):
            return None

    def _save_meta(self, meta: dict) -> None:
        path = self._dir(meta["id"]) / "meta.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta))
        os.replace(tmp, path)

    def ids(self) -> list[int]:
        if not self.dir.exists():
            return []
        return sorted(int(p.name) for p in self.dir.iterdir() if p.name.isdigit())

    def start(self, launch: list[str], shown: list[str], cwd: Path, why: str = "") -> int:
        """Launch a command detached. `launch` is what runs (possibly behind sandbox-exec), `shown` what Elijah would
        recognise. Returns the job id."""
        self.dir.mkdir(parents=True, exist_ok=True)
        job_id = max(self.ids(), default=0) + 1
        while True:  # claim the folder; another process may have just taken this number
            try:
                self._dir(job_id).mkdir()
                break
            except FileExistsError:
                job_id += 1
        folder = self._dir(job_id)
        (folder / "job.log").touch()
        proc = subprocess.Popen([sys.executable, "-I", "-c", WRAPPER, str(folder), *launch], cwd=cwd,
                                env=sandbox.scrubbed_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        self._save_meta({"id": job_id, "argv": list(shown), "cwd": str(cwd), "pid": proc.pid,
                         "started": time.time(), "why": why[:300], "notified": {}})
        return job_id

    @staticmethod
    def _alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        try:  # a finished child of this process lingers as a zombie until reaped
            done, _ = os.waitpid(pid, os.WNOHANG)
            return done == 0
        except ChildProcessError:
            return True  # not our child: it exists, so it's running

    @staticmethod
    def _tail(path: Path, lines: int) -> str:
        try:
            with path.open("rb") as f:
                f.seek(max(0, path.stat().st_size - 64_000))
                text = f.read().decode(errors="replace")
        except OSError:
            return ""
        return sandbox.redact("\n".join(text.splitlines()[-lines:])[-TAIL_CHARS:])

    def status(self, job_id: int, lines: int = TAIL_LINES, now: float | None = None) -> dict | None:
        """State (running / exited / lost), exit code, runtime, last output time and the log's tail."""
        meta = self._meta(job_id)
        if not meta:
            return None
        now = now or time.time()
        folder = self._dir(job_id)
        exit_file, logfile = folder / "exit", folder / "job.log"
        code = None
        if exit_file.exists():
            try:
                code = int(exit_file.read_text().strip())
            except ValueError:
                code = -1
            state, ended = "exited", exit_file.stat().st_mtime
        elif self._alive(meta["pid"]):
            state, ended = "running", None
        else:  # killed without the wrapper noticing (a restart, a kill -9)
            state, ended = "lost", logfile.stat().st_mtime if logfile.exists() else meta["started"]
        last_output = logfile.stat().st_mtime if logfile.exists() and logfile.stat().st_size else None
        return {**meta, "state": state, "code": code, "runtime": (ended or now) - meta["started"],
                "last_output": last_output, "tail": self._tail(logfile, lines)}

    def recent(self, limit: int = 10) -> list[dict]:
        """Newest first."""
        out = []
        for job_id in reversed(self.ids()):
            if (s := self.status(job_id, lines=0)):
                out.append(s)
            if len(out) >= limit:
                break
        return out

    def line(self, s: dict) -> str:
        return f"#{s['id']} [{describe_state(s)}] $ {command_text(s['argv'])[:120]} (in {s['cwd']})"

    def kill(self, job_id: int) -> bool:
        s = self.status(job_id, lines=0)
        if not s or s["state"] != "running":
            return False
        try:
            os.killpg(s["pid"], signal.SIGTERM)  # the wrapper leads its own session: this reaches the command too
        except (ProcessLookupError, PermissionError):
            return False
        return True

    def pending(self) -> bool:
        """Whether any job still has something to report (it hasn't ended and been announced)."""
        return any((m := self._meta(i)) and not m["notified"].get("end") for i in self.ids())

    def check(self, notify: Callable[[str], None], stall_minutes: int, now: float | None = None) -> int:
        """Say one thing per event: a job exited or was lost, or has been quiet for `stall_minutes`."""
        now = now or time.time()
        raised = 0
        for job_id in self.ids():
            meta = self._meta(job_id)
            if not meta or meta["notified"].get("end"):
                continue
            s = self.status(job_id, lines=8, now=now)
            event, message = None, None
            if s["state"] in ("exited", "lost"):
                event, mark = "end", "✅" if s["code"] == 0 else "❌"
                tail = s["tail"].splitlines()[-3:]
                message = (f"{mark} Background job {describe_state(s)}: $ {command_text(s['argv'])[:150]}"
                           + ("\n" + "\n".join(tail) if tail else ""))
                if s["code"] != 0 and WANTED_INPUT.search(s["tail"]):
                    message += ("\n(It stopped because it was waiting for typed input, which a background job can't "
                                "give. It needs its answers as arguments, environment variables or a file instead.)")
            elif not meta["notified"].get("stall"):
                since = s["last_output"] or meta["started"]
                if now - since >= stall_minutes * 60:
                    event = "stall"
                    message = (f"A background job has printed nothing for {duration(now - since)} (still running "
                               f"{duration(s['runtime'])}): $ {command_text(s['argv'])[:150]}")
            if event:
                meta["notified"][event] = True
                self._save_meta(meta)  # marked first: a failing send must not repeat on every check
                notify(message[:1500])
                raised += 1
        return raised

    def prune(self, now: float | None = None) -> int:
        """Remove finished jobs older than KEEP_DAYS."""
        now = now or time.time()
        removed = 0
        for job_id in self.ids():
            s = self.status(job_id, lines=0, now=now)
            if s and s["state"] != "running" and now - s["started"] > KEEP_DAYS * 86400:
                shutil.rmtree(self._dir(job_id), ignore_errors=True)
                removed += 1
        return removed


class Watcher:
    """The thread that calls Jobs.check every CHECK_EVERY seconds while a job is running, and sleeps otherwise."""

    def __init__(self, jobs: Jobs, notify: Callable[[str], None], stall_minutes: Callable[[], int],
                 every: float | None = None) -> None:
        self.jobs, self.notify, self.stall_minutes = jobs, notify, stall_minutes
        self.every = CHECK_EVERY if every is None else every
        self._wake, self._stop = threading.Event(), threading.Event()
        self._thread = threading.Thread(target=self._run, name="terminal-jobs", daemon=True)

    def start(self) -> None:
        self.jobs.prune()
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                busy = self.jobs.pending()
                if busy:
                    self.jobs.check(self.notify, self.stall_minutes())
            except Exception:
                log.exception("the job watcher failed")
                busy = True
            self._wake.wait(self.every if busy else None)  # nothing to watch: sleep until a job starts
            self._wake.clear()
