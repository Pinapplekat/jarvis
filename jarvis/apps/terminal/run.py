"""Running a graded command: in its sandbox when it runs on its own, as Elijah (no sandbox, still no shell) when he
said yes. `rm` inside a project is a move to the Trash instead, with an undo. Every run is audited."""

from __future__ import annotations

import json
import shlex
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from jarvis.apps.terminal import sandbox
from jarvis.apps.terminal.grade import Verdict
from jarvis.apps.terminal.sandbox import Result

AUTO_TIMEOUT, SLOW_TIMEOUT, APPROVED_TIMEOUT = 30, 120, 120
TRASH_DIR = Path.home() / ".Trash"  # tests point this somewhere else


def profile_for(verdict: Verdict) -> str | None:
    """The sandbox a command that runs on its own gets (None: it can't be sandboxed, like ps)."""
    if not verdict.sandboxed:
        return None
    if verdict.sandbox in ("write", "git"):
        return sandbox.write_sandbox(Path(verdict.root), git=verdict.sandbox == "git")
    return sandbox.read_sandbox()


def audit(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry}
    with path.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def run_command(verdict: Verdict, cwd: Path, log: Path, approved: bool) -> Result:
    """Run a classified command. Without Elijah's yes it must be an `auto` one and runs in its sandbox."""
    if not approved and verdict.kind != "auto":
        raise ValueError("this command needs Elijah's OK")
    slow = "pytest" in verdict.argv[:4] or verdict.risk == "medium"
    timeout = SLOW_TIMEOUT if slow else APPROVED_TIMEOUT if approved else AUTO_TIMEOUT
    result = sandbox.run_argv(verdict.argv, cwd, timeout, None if approved else profile_for(verdict))
    audit(log, {"argv": verdict.argv, "cwd": str(cwd), "kind": verdict.kind, "risk": verdict.risk,
                "reason": verdict.reason, "by": "elijah" if approved else "auto", "exit": result.code,
                "timed_out": result.timed_out, "output_chars": len(result.output)})  # never the output itself
    return result


def describe(result: Result) -> str:
    status = "timed out" if result.timed_out else ("couldn't run" if result.code is None else f"exit {result.code}")
    return f"[{status}]\n{result.output}"


def command_text(argv: list[str]) -> str:
    return shlex.join(argv)


# ---- the Trash ------------------------------------------------------------------------------------------

def move_to_trash(paths: tuple[Path, ...]) -> tuple[list[list[str]], list[Path]]:
    """Move each existing path into ~/.Trash under a name that isn't taken. Returns ([trash path, original]
    pairs, the paths that didn't exist)."""
    TRASH_DIR.mkdir(parents=True, exist_ok=True)
    moved, missing = [], []
    for path in paths:
        if not path.exists() and not path.is_symlink():
            missing.append(path)
            continue
        target = TRASH_DIR / path.name
        if target.exists() or target.is_symlink():
            target = TRASH_DIR / f"{path.stem} {time.strftime('%H.%M.%S')}-{time.time_ns() % 10**6}{path.suffix}"
        shutil.move(str(path), str(target))
        moved.append([str(target), str(path)])
    return moved, missing


def restore_from_trash(moves: list[list[str]]) -> tuple[list[str], list[str]]:
    """Move things back. Returns (restored, what couldn't be: gone from the Trash or the place is taken)."""
    restored, failed = [], []
    for trashed, original in moves:
        src, dest = Path(trashed), Path(original)
        if not (src.exists() or src.is_symlink()):
            failed.append(f"{dest.name} is no longer in the Trash")
        elif dest.exists() or dest.is_symlink():
            failed.append(f"{dest.name} can't go back: something is already at {dest}")
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
            restored.append(dest.name)
    return restored, failed
