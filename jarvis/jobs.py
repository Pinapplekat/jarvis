"""Background jobs: approved commands that keep running after the 120s cutoff.

A job is a command Elijah tapped Run on, launched detached in its own session with output
going to a log under Application Support/JARVIS/jobs/<id>/. A tiny wrapper process waits for
it and writes the exit code, so JARVIS can tell later whether it finished and how. Reading a
job's status needs no approval; the log text is untrusted data. The tick files one report to
JARVIS (jarvis.reports) when a job exits, dies, or goes quiet for JOB_STALL_MINUTES, and JARVIS
tells Elijah in its own words.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from jarvis import config
from jarvis.memory import Memory
from jarvis.screen import redact

JOBS_DIR = config.DATA_DIR / "jobs"
TAIL_LINES, TAIL_CHARS = 20, 3000
KEEP_DAYS = 14  # finished job folders older than this are removed

# Runs the command, then writes its exit code. Plain argv, no shell; started with -I so
# nothing in the job's folder can be imported in its place.
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


def _dir(job_id: int) -> Path:
    return JOBS_DIR / str(int(job_id))


def _meta(job_id: int) -> dict | None:
    try:
        return json.loads((_dir(job_id) / "meta.json").read_text())
    except (OSError, ValueError):
        return None


def _save_meta(meta: dict) -> None:
    path = _dir(meta["id"]) / "meta.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta))
    os.replace(tmp, path)


def _ids() -> list[int]:
    if not JOBS_DIR.exists():
        return []
    return sorted(int(p.name) for p in JOBS_DIR.iterdir() if p.name.isdigit())


def start_job(argv: list[str], cwd: str | Path, action_id: int | None = None, why: str = "") -> int:
    """Launch an approved command detached. Returns the job id. Only call this after Elijah's Run."""
    from jarvis.introspect import scrubbed_env
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    job_id = (max(_ids(), default=0)) + 1
    while True:  # claim the folder; another process may have just taken this number
        try:
            _dir(job_id).mkdir()
            break
        except FileExistsError:
            job_id += 1
    folder = _dir(job_id)
    (folder / "job.log").touch()
    proc = subprocess.Popen([sys.executable, "-I", "-c", WRAPPER, str(folder), *argv],
                            cwd=Path(cwd).expanduser(), env=scrubbed_env(), stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    _save_meta({"id": job_id, "argv": list(argv), "cwd": str(cwd), "pid": proc.pid, "started": time.time(),
                "action_id": action_id, "why": why[:300], "notified": {}})
    return job_id


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


def _tail(log: Path, lines: int) -> str:
    try:
        with log.open("rb") as f:
            f.seek(max(0, log.stat().st_size - 64_000))
            text = f.read().decode(errors="replace")
    except OSError:
        return ""
    out = "\n".join(text.splitlines()[-lines:])
    return redact(out[-TAIL_CHARS:])


def job_status(job_id: int, lines: int = TAIL_LINES, now: float | None = None) -> dict | None:
    """State (running / exited / lost), exit code, runtime, last output time and the log's tail."""
    meta = _meta(job_id)
    if not meta:
        return None
    now = now or time.time()
    folder = _dir(job_id)
    exit_file, log = folder / "exit", folder / "job.log"
    code = None
    if exit_file.exists():
        try:
            code = int(exit_file.read_text().strip())
        except ValueError:
            code = -1
        state, ended = "exited", exit_file.stat().st_mtime
    elif _alive(meta["pid"]):
        state, ended = "running", None
    else:  # killed without the wrapper noticing (a restart, a kill -9)
        state, ended = "lost", log.stat().st_mtime if log.exists() else meta["started"]
    last_output = log.stat().st_mtime if log.exists() and log.stat().st_size else None
    return {**meta, "state": state, "code": code, "runtime": (ended or now) - meta["started"],
            "last_output": last_output, "tail": _tail(log, lines)}


def list_jobs(limit: int = 10) -> list[dict]:
    """Newest first."""
    out = []
    for job_id in reversed(_ids()):
        if (s := job_status(job_id, lines=0)):
            out.append(s)
        if len(out) >= limit:
            break
    return out


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"


def describe_state(s: dict) -> str:
    if s["state"] == "running":
        return f"running {_duration(s['runtime'])}"
    if s["state"] == "lost":
        return f"stopped without an exit code after {_duration(s['runtime'])}"
    code = s["code"]
    how = "finished OK" if code == 0 else f"killed by signal {-code}" if code < 0 else f"failed (exit {code})"
    return f"{how} after {_duration(s['runtime'])}"


def _command(s: dict) -> str:
    import shlex
    return shlex.join(s["argv"])


def status_text(job_id: int | None = None, now: float | None = None) -> str:
    """For chat: one job's state plus its log tail (the newest job if no id)."""
    if job_id is None:
        ids = _ids()
        if not ids:
            return "(no background jobs yet)"
        job_id = ids[-1]
    s = job_status(job_id, now=now)
    if not s:
        return f"(no job #{job_id})"
    now = now or time.time()
    quiet = f", last output {_duration(now - s['last_output'])} ago" if s["last_output"] else ", no output yet"
    return (f"job #{s['id']}: {describe_state(s)}{quiet}\n$ {_command(s)}\nin {s['cwd']}\n"
            f"last log lines:\n{s['tail'] or '(empty)'}")


def for_prompt(limit: int = 5) -> str:
    lines = [f"- job #{s['id']} [{describe_state(s)}] $ {_command(s)[:120]}" for s in list_jobs(limit)]
    return "\n".join(lines) or "(none)"


def kill_job(job_id: int) -> bool:
    s = job_status(job_id, lines=0)
    if not s or s["state"] != "running":
        return False
    try:
        os.killpg(s["pid"], signal.SIGTERM)  # the wrapper leads its own session: this reaches the command too
    except (ProcessLookupError, PermissionError):
        return False
    return True


def check_jobs(mem: Memory, now: float | None = None) -> int:
    """File one report per event: a job exited, was lost, or has been quiet for JOB_STALL_MINUTES.
    JARVIS passes it on (bundled with anything else that came in)."""
    from jarvis import reports
    now = now or time.time()
    raised = 0
    for job_id in _ids():
        meta = _meta(job_id)
        if not meta or meta["notified"].get("end"):
            continue
        s = job_status(job_id, lines=8, now=now)
        event, message, kind = None, None, "note"
        if s["state"] in ("exited", "lost"):
            event = "end"
            kind = "done" if s["code"] == 0 else "failed"
            tail = s["tail"].splitlines()[-3:]
            message = (f"Background job {describe_state(s)}: $ {_command(s)[:150]}"
                       + ("\n" + "\n".join(tail) if tail else ""))
        elif not meta["notified"].get("stall"):
            since = s["last_output"] or meta["started"]
            if now - since >= config.JOB_STALL_MINUTES * 60:
                event = "stall"
                message = (f"A background job has printed nothing for {_duration(now - since)} "
                           f"(still running {_duration(s['runtime'])}): $ {_command(s)[:150]}")
        if event:
            reports.add(mem, "job", job_id, kind, f"the background job `{_command(s)[:60]}`", message[:1500])
            raised += 1
            meta["notified"][event] = True
            _save_meta(meta)
            mem.log("job", f"#{job_id} {event}: {describe_state(s)}")
    return raised


def prune(now: float | None = None) -> int:
    """Remove finished jobs older than KEEP_DAYS."""
    import shutil
    now = now or time.time()
    removed = 0
    for job_id in _ids():
        s = job_status(job_id, lines=0, now=now)
        if s and s["state"] != "running" and now - s["started"] > KEEP_DAYS * 86400:
            shutil.rmtree(_dir(job_id), ignore_errors=True)
            removed += 1
    return removed
