"""Terminal on the Mac node: commands graded by risk, background jobs, Claude Code tasks, git repos.

Commands, jobs and tasks are one app because they share the grading and the sandbox, and an app can't import
another app. The work is in grade.py, sandbox.py, run.py, jobs.py, agent.py, tasks.py and repos.py; this file is the
handlers.
"""

from __future__ import annotations

import os
from pathlib import Path

from jarvis.apps.terminal import repos, run as runner
from jarvis.apps.terminal.folders import Folders
from jarvis.apps.terminal.grade import Verdict, classify, parse
from jarvis.apps.terminal.jobs import Jobs, Watcher
from jarvis.apps.terminal.sandbox import is_secret_path, redact
from jarvis.apps.terminal.tasks import ACTIVE, Tasks, TaskRunner, check_folder, progress_text
from jarvis.contract.api import ActionError, AppContext, Result

_tasks: dict[Path, Tasks] = {}  # one Tasks (and so one lock) per folder, shared by the handlers and the worker
_service: "Service | None" = None


class Service:
    """The background threads: the job watcher and the task worker."""

    def __init__(self, ctx: AppContext) -> None:
        self.watcher = Watcher(_jobs(ctx), ctx.notify, lambda: ctx.settings["job_stall_minutes"])
        self.worker = TaskRunner(_task_store(ctx), lambda: Folders.from_settings(ctx.settings), ctx.settings,
                                 ctx.notify)


def start(ctx: AppContext) -> None:
    global _service
    _service = Service(ctx)
    _service.watcher.start()
    _service.worker.start()


def stop(ctx: AppContext) -> None:
    global _service
    if _service is not None:
        _service.watcher.stop()
        _service.worker.stop()
        _service = None


# ---- helpers -----------------------------------------------------------------------------------------

def _jobs(ctx: AppContext) -> Jobs:
    return Jobs(ctx.data_dir / "jobs")


def _task_store(ctx: AppContext) -> Tasks:
    return _tasks.setdefault(ctx.data_dir / "tasks", Tasks(ctx.data_dir / "tasks"))


def _log(ctx: AppContext) -> Path:
    return ctx.data_dir / "commands.jsonl"


def _folder(text: str, folders: Folders) -> Path:
    """A folder given as a path, or as a project folder name under one of the work roots."""
    raw = os.path.expanduser(text.strip())
    path = Path(raw)
    if not path.is_absolute():
        path = next((root / raw for root in folders.work_roots if (root / raw).is_dir()), Path.cwd() / raw)
    path = path.resolve()
    if not path.is_dir():
        raise ActionError(f"There is no folder {text}.")
    return path


def _grade(ctx: AppContext, command: str, cwd: str) -> tuple[Verdict, Path, Folders]:
    folders = Folders.from_settings(ctx.settings)
    where = _folder(cwd, folders) if cwd.strip() else Path.home()
    verdict = classify(command, where, folders)
    if verdict.kind == "reject":
        raise ActionError(f"Refused: {verdict.reason}.")
    return verdict, where, folders


def _to_trash(ctx: AppContext, verdict: Verdict) -> Result:
    moved, missing = runner.move_to_trash(verdict.trash)
    runner.audit(_log(ctx), {"argv": verdict.argv, "kind": "trash", "risk": verdict.risk, "by": "auto",
                             "moved": len(moved)})
    if not moved:
        raise ActionError("Nothing to delete: " + ", ".join(p.name for p in missing) + " doesn't exist.")
    names = ", ".join(Path(o).name for _, o in moved)
    note = f" ({', '.join(p.name for p in missing)} didn't exist)" if missing else ""
    return Result(f"Moved {names} to the Trash{note}. It can be put back with undo.", {"moved": [o for _, o in moved]},
                  undo={"moves": moved}, subject=f"trash:{moved[0][1]}")


def _start_job(ctx: AppContext, verdict: Verdict, cwd: Path, approved: bool, why: str = "") -> Result:
    profile = None if approved else runner.profile_for(verdict)
    argv = ["/usr/bin/sandbox-exec", "-p", profile, *verdict.argv] if profile else verdict.argv
    job_id = _jobs(ctx).start(argv, verdict.argv, cwd, why)
    runner.audit(_log(ctx), {"argv": verdict.argv, "cwd": str(cwd), "kind": verdict.kind, "risk": verdict.risk,
                             "by": "elijah" if approved else "auto", "job": job_id})
    if _service is not None:
        _service.watcher.wake()
    return Result(f"Started background job #{job_id}: $ {runner.command_text(verdict.argv)}. Elijah is told when it "
                  "ends or goes quiet.", {"job": job_id})


def _ran(result, verdict: Verdict) -> Result:
    return Result(runner.describe(result), {"exit": result.code, "timed_out": result.timed_out})


# ---- commands ----------------------------------------------------------------------------------------

def run(ctx: AppContext, command: str, cwd: str = "", background: bool = False) -> Result:
    verdict, where, _ = _grade(ctx, command, cwd)
    if verdict.kind == "trash":
        return _to_trash(ctx, verdict)
    if verdict.kind != "auto":
        runner.audit(_log(ctx), {"argv": verdict.argv, "cwd": str(where), "kind": "approval", "risk": verdict.risk,
                                 "reason": verdict.reason, "by": None})
        return Result(f"This needs Elijah's OK ({verdict.reason}): use run_with_ok with a short why.",
                      {"needs_ok": True, "risk": verdict.risk})
    if background:
        return _start_job(ctx, verdict, where, approved=False)
    return _ran(runner.run_command(verdict, where, _log(ctx), approved=False), verdict)


def run_with_ok(ctx: AppContext, command: str, cwd: str, why: str, background: bool = False) -> Result:
    """Elijah said yes (the host asked with the exact command): run it as him, no sandbox, still no shell."""
    verdict, where, _ = _grade(ctx, command, cwd)
    if verdict.kind == "trash":
        return _to_trash(ctx, verdict)
    verdict = Verdict(verdict.kind, verdict.reason, parse(command), sandboxed=False, risk=verdict.risk, sandbox="")
    if background:
        return _start_job(ctx, verdict, where, approved=True, why=why)
    return _ran(runner.run_command(verdict, where, _log(ctx), approved=True), verdict)


def undo_run(ctx: AppContext, args: dict) -> Result:
    restored, failed = runner.restore_from_trash(args["moves"])
    if not restored:
        raise ActionError("; ".join(failed) or "Nothing to put back.")
    return Result(f"Put back {', '.join(restored)}." + (f" ({'; '.join(failed)})" if failed else ""))


# ---- jobs --------------------------------------------------------------------------------------------

def jobs(ctx: AppContext) -> Result:
    store = _jobs(ctx)
    rows = store.recent(10)
    return Result("\n".join(store.line(s) for s in rows) or "No background jobs yet.", {"jobs": [s["id"] for s in rows]})


def job_log(ctx: AppContext, id: int, lines: int = 40) -> Result:
    s = _jobs(ctx).status(id, lines=lines)
    if s is None:
        raise ActionError(f"There is no job #{id}.")
    return Result(f"{_jobs(ctx).line(s)}\n{s['tail'] or '(no output yet)'}")


def stop_job(ctx: AppContext, id: int) -> Result:
    if not _jobs(ctx).kill(id):
        raise ActionError(f"Job #{id} isn't running.")
    return Result(f"Stopping job #{id}; Elijah is told when it has stopped.")


# ---- tasks -------------------------------------------------------------------------------------------

def start_task(ctx: AppContext, goal: str, folder: str, plan: str) -> Result:
    """The host asked Elijah about the plan before this ran."""
    folders = Folders.from_settings(ctx.settings)
    try:
        path = check_folder(str(_folder(folder, folders)), folders)
    except ValueError as e:
        raise ActionError(str(e)) from None
    task = _task_store(ctx).create(goal, path, plan)
    if _service is not None:
        _service.worker.wake()
    where = " (read-only: Falak's own folder, so it can only look)" if folders.is_read_only(path) else ""
    return Result(f"Task #{task['id']} queued in {path}{where}. Elijah gets one message when it ends.",
                  {"task": task["id"]})


def _task(ctx: AppContext, id: int, active_only: bool = False) -> dict:
    store = _task_store(ctx)
    task = store.get(id) if id else store.newest(active_only)
    if task is None:
        raise ActionError("There is no task running." if active_only and not id else
                          "There are no tasks yet." if not id else f"There is no task #{id}.")
    return task


def task_status(ctx: AppContext, id: int = 0) -> Result:
    t = _task(ctx, id)
    return Result(redact(progress_text(t)), {"id": t["id"], "status": t["status"]})


def stop_task(ctx: AppContext, id: int = 0) -> Result:
    t = _task(ctx, id, active_only=True)
    if t["status"] not in ACTIVE or _task_store(ctx).stop(t["id"]) is None:
        raise ActionError(f"Task #{t['id']} isn't running.")
    return Result(f"Stopped task #{t['id']} ({t['goal'][:60]}).")


# ---- repos (shared with the projects app) ------------------------------------------------------------

def find_repos(ctx: AppContext) -> Result:
    found = repos.discover(Folders.from_settings(ctx.settings))
    return Result(f"{len(found)} repos", {"repos": found})


def repo_state(ctx: AppContext, path: str) -> Result:
    folder = Path(os.path.expanduser(path)).resolve()
    if not folder.is_dir() or is_secret_path(folder):
        raise ActionError(f"{path} isn't a folder I can look at.")
    state = repos.repo_state(folder)
    if state is None:
        raise ActionError(f"{path} isn't a git repo (or git can't read it right now).")
    return Result(repos.state_text(state), state)
