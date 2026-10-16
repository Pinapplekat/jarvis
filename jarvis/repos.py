"""Git sense: find Elijah's repos, notice new commits and branch switches, and tell which
project something belongs to."""

import json
import subprocess
from pathlib import Path

from jarvis import config
from jarvis.memory import Memory

SKIP_DIRS = {"Library", "node_modules", ".Trash", "Applications", ".venv", ".venv.nosync", "venv",
             "build", "dist", "target", "Pictures", "Music", "Movies"}
REPOS_KEY, STATE_KEY = "repos", "git_state"


def discover(root: Path | None = None, max_depth: int = 4) -> list[str]:
    """Paths of git repos under the watch roots (default: home), skipping heavy/system dirs.
    The iCloud Drive folder is included explicitly since it lives under ~/Library."""
    roots = [Path(p).expanduser() for p in config.WATCH_ROOTS.split(",") if p.strip()] if root is None else [root]
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

    for r in roots:
        if r.exists():
            walk(r, 0)
    return sorted(set(found))


def known_repos(mem: Memory) -> list[str]:
    raw = mem.get_kv(REPOS_KEY)
    return json.loads(raw) if raw else []


def refresh_repos(mem: Memory) -> list[str]:
    repos = discover()
    mem.set_kv(REPOS_KEY, json.dumps(repos))
    return repos


def project_for(repos: list[str], cwd: str | None = None, text: str | None = None) -> str | None:
    """Repo name for a working directory, or a repo name mentioned in a window title."""
    if cwd:
        for r in sorted(repos, key=len, reverse=True):
            if cwd == r or cwd.startswith(r + "/"):
                return Path(r).name
    if text:
        lowered = text.lower()
        for r in sorted(repos, key=lambda p: len(Path(p).name), reverse=True):
            name = Path(r).name
            if len(name) >= 3 and name.lower() in lowered:
                return name
    return None


def _git(repo: str, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def head_state(repo: str) -> dict | None:
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    log = _git(repo, "log", "-1", "--format=%H%x1f%s")
    if branch is None or log is None:
        return None  # empty repo, or unreadable right now (e.g. iCloud busy)
    sha, _, subject = log.partition("\x1f")
    return {"branch": branch, "sha": sha, "subject": subject}


def check_git(mem: Memory, state_of=head_state) -> int:
    """Record new commits and branch switches since the last check. Returns events recorded."""
    previous = json.loads(mem.get_kv(STATE_KEY) or "{}")
    current, events = {}, 0
    for repo in known_repos(mem):
        state = state_of(repo)
        if state is None:
            if repo in previous:
                current[repo] = previous[repo]
            continue
        current[repo] = state
        before = previous.get(repo)
        name = Path(repo).name
        if before is None:
            continue  # first sighting: baseline only
        if state["branch"] != before["branch"]:
            mem.observe("git", f"switched to branch {state['branch']}", project=name)
            events += 1
        if state["sha"] != before["sha"]:
            mem.observe("git", f"new commit on {state['branch']}: {state['subject'][:150]}", project=name)
            events += 1
    mem.set_kv(STATE_KEY, json.dumps(current))
    return events
