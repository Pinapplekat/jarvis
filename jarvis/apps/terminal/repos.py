"""Finding Elijah's git repos and reading their state. Ported from legacy/jarvis/repos.py (`discover`)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from jarvis.apps.terminal.folders import Folders

SKIP_DIRS = {"Library", "node_modules", ".Trash", "Applications", ".venv", ".venv.nosync", "venv", "build", "dist",
             "target", "Pictures", "Music", "Movies"}


def discover(folders: Folders, max_depth: int = 4) -> list[str]:
    """Paths of git repos under the work roots, skipping heavy and system folders."""
    found: list[str] = []

    def walk(d: Path, depth: int) -> None:
        if (d / ".git").exists():
            found.append(str(d))
            return
        if depth >= max_depth:
            return
        try:
            children = [c for c in d.iterdir() if c.is_dir() and not c.is_symlink()]
        except OSError:
            return
        for c in children:
            if c.name in SKIP_DIRS or (c.name.startswith(".") and c.name != ".config"):
                continue
            walk(c, depth + 1)

    for root in folders.work_roots:
        if root.exists():
            walk(root, 0)
    return sorted(set(found))


def _git(repo: Path, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", "--no-optional-locks", "-C", str(repo), *args], capture_output=True, text=True,
                           timeout=15, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def repo_state(repo: Path) -> dict | None:
    """Branch, last commit, uncommitted files and ahead/behind upstream. None if it isn't a readable repo."""
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if branch is None:
        return None
    last = _git(repo, "log", "-1", "--format=%s%x1f%cI")
    subject, _, date = (last or "").partition("\x1f")
    status = _git(repo, "status", "--porcelain") or ""
    counts = _git(repo, "rev-list", "--left-right", "--count", "@{upstream}...HEAD")
    behind = ahead = None
    if counts:
        behind, ahead = (int(n) for n in counts.split())
    return {"path": str(repo), "branch": branch, "subject": subject or None, "date": date or None,
            "changed": len([ln for ln in status.splitlines() if ln.strip()]), "ahead": ahead, "behind": behind}


def state_text(s: dict) -> str:
    last = f'last commit "{s["subject"]}" ({s["date"][:10]})' if s["subject"] else "no commits yet"
    where = f", {s['ahead']} ahead and {s['behind']} behind its upstream" if s["ahead"] is not None else ""
    changed = f"{s['changed']} uncommitted file(s)" if s["changed"] else "nothing uncommitted"
    return f"{Path(s['path']).name}: on {s['branch']}, {last}, {changed}{where}"
