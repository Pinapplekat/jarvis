"""Grading a proposed command by risk. Ported from legacy/jarvis/terminal.py, with the grades the real approval
history showed were wrong fixed (git -C, open, lsof, sed -n, rm to the Trash).

Low (reading, anywhere but secrets) runs in the no-writes, no-network sandbox. Medium (ordinary work inside the
project) runs in a sandbox that can write only there. High and critical (push, reset, sudo, installs elsewhere,
anything that reaches other people, anything not recognised) need Elijah's yes. Naming a secret is refused whatever
the grade, and shell syntax is refused, never interpreted.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from jarvis.apps.terminal.folders import Folders, inside
from jarvis.apps.terminal.sandbox import is_secret, is_secret_path
from jarvis.apps.terminal.tables import (
    SHELL_CHARS, READERS, LOOKUPS, HELP_FLAGS,
    GIT_READ, GIT_WRITE, GIT_BRANCH_LIST, GIT_REFUSED,
    FILE_WRITERS, RUNNERS, UV_OK, NPM_OK,
    CRITICAL, BLOCKED_FLAGS, GREP_EXCLUDES, UV_VALUE_FLAGS,
    OPEN_FLAGS, OPEN_RISKY, LSOF_SIMPLE, LSOF_VALUED,
    PUBLISH, REACHES_OUT, OSASCRIPT_DANGER,
)

@dataclass
class Verdict:
    kind: str  # auto | approval | reject | trash
    reason: str
    argv: list[str]  # what will actually run (auto commands get safety flags added)
    sandboxed: bool = True
    risk: str = "high"  # low | medium | high | critical
    sandbox: str = "read"  # read (no writes, no network) | write | git (writes only inside `root`) | "" (none)
    root: str = ""  # the folder a write/git sandbox may write to
    trash: tuple[Path, ...] = ()  # kind == "trash": what `rm` would delete, moved to ~/.Trash instead


def parse(command: object) -> list[str]:
    """A command string or argv list -> argv. Shell syntax is refused, never interpreted."""
    if isinstance(command, list):
        argv = [str(a) for a in command]
    else:
        text = str(command).strip()
        if _shell_syntax(text):
            raise ValueError("shell syntax (pipes, redirects, $, ;, &&) isn't supported; one plain command only")
        try:
            argv = shlex.split(text)
        except ValueError as e:
            raise ValueError(f"can't read that command ({e})") from None
    if not argv or not argv[0].strip():
        raise ValueError("empty command")
    if any("\0" in a or "\n" in a for a in argv):
        raise ValueError("bad characters in command")
    return [os.path.expanduser(a) if a == "~" or a.startswith("~/") else a for a in argv]  # no shell to do it


def _shell_syntax(text: str) -> bool:
    """Shell characters outside quotes. Quoted ones are plain text to a shell too (a commit message with
    `<noreply@…>`), except `$` and backticks inside double quotes, which a task's shell would still expand."""
    quote, escaped = "", False
    for c in text:
        if c == "\n":
            return True
        if escaped:
            escaped = False
        elif quote == "'":
            quote = "" if c == "'" else quote
        elif c == "\\":
            escaped = True
        elif quote == '"':
            if c in "$`":
                return True
            quote = "" if c == '"' else quote
        elif c in "'\"":
            quote = c
        elif c in SHELL_CHARS:
            return True
    return False


def full_path(arg: str, cwd: Path) -> Path:
    raw = Path(os.path.expanduser(arg))
    return (raw if raw.is_absolute() else cwd / raw).resolve()  # follows symlinks, collapses ..


def _blocked_flag(tool: str, args: list[str]) -> str | None:
    for a in args:
        for flag in BLOCKED_FLAGS.get(tool, ()):
            if a == flag or (flag.startswith("--") and a.startswith(flag + "=")):
                return a
            if len(flag) == 2 and a.startswith("-") and not a.startswith("--") and flag[1] in a[1:]:
                return a  # short flags can hide in a cluster: `tail -nf`, `grep -rf list`
    return None


def _secret_arg(args: list[str], cwd: Path) -> str | None:
    """The first argument naming something secret (by name, folder, or where a symlink leads). Plain words that
    aren't paths (a grep pattern like TOKEN) don't count."""
    for a in args:
        if a.startswith("-") and "=" not in a:
            continue
        value = a.partition("=")[2] if a.startswith("-") else a
        if not value:
            continue
        full = full_path(value, cwd)
        looks_like_path = "/" in value or ":" in value or value.startswith(("~", ".")) or full.exists()
        if looks_like_path and (is_secret(Path(value.replace(":", "/"))) or is_secret_path(full)):
            return a
    return None


def _uv_inner(args: list[str]) -> list[str]:
    """For `uv run [flags] cmd ...`: the command it runs."""
    i = 1
    while i < len(args):
        if args[i] in UV_VALUE_FLAGS:
            i += 2
        elif args[i].startswith("-"):
            i += 1
        else:
            return args[i:]
    return []


def _outside(args: list[str], cwd: Path, root: Path) -> str | None:
    """The first path argument that lands outside `root` or inside its .git, or None."""
    for a in args:
        if a.startswith("-"):
            continue
        full = full_path(a, cwd)
        if not inside(full, root) or ".git" in full.relative_to(root).parts:
            return a
    return None


def _flags(args: list[str]) -> set[str]:
    """Single-letter flags, clusters split: ['-rf', '--force'] -> {'r', 'f', '--force'}."""
    out = set()
    for a in args:
        if a.startswith("--"):
            out.add(a)
        elif a.startswith("-") and len(a) > 1:
            out.update(a[1:])
    return out


def _approval(risk: str, reason: str, argv: list[str]) -> Verdict:
    return Verdict("approval", reason, argv, sandboxed=False, risk=risk, sandbox="")


def classify(command: object, cwd: Path, folders: Folders) -> Verdict:
    """Grade a proposed command. Anything not recognised is high."""
    try:
        argv = parse(command)
    except ValueError as e:
        return Verdict("reject", str(e), [])
    where = Path(cwd).expanduser().resolve()
    if (bad := _secret_arg(argv, where)) or _secret_arg([str(where)], where):
        return Verdict("reject", f"{bad or 'that folder'} may hold secrets; never opened", [])
    tool, args = Path(argv[0]).name, argv[1:]
    if argv[0] != tool:  # an explicit path to a program: let Elijah look at it
        return _approval("high", "runs a program by path", argv)
    return _grade(argv, tool, args, where, folders)


def _grade(argv: list[str], tool: str, args: list[str], where: Path, folders: Folders) -> Verdict:
    root = folders.writable_root(where)
    away = "a read-only folder" if folders.project_root(where) else "outside your work folders"

    if tool in CRITICAL:
        return _approval("critical", f"{tool} changes the system, credentials or reaches other people", argv)
    if tool in ("rm", "chmod", "mv") and _flags(args) & {"r", "R", "--recursive"} and (
            root is None or any(not a.startswith("-") and not inside(full_path(a, where), root) for a in args)):
        return _approval("critical", f"recursive {tool} outside the project", argv)
    if len(args) in (1, 2) and args[-1] in HELP_FLAGS and all(not a.startswith("/") for a in args):
        if shutil.which(tool):  # an installed tool explaining itself, sandboxed
            return Verdict("auto", f"{tool} help/version", argv, risk="low")
    if tool == "git":
        return _git(argv, args, where, folders)
    if tool == "open":
        return _open(argv, args, where)
    if tool == "lsof":
        return _lsof(argv, args)
    if tool == "rm":
        return _rm(argv, args, where, root, away)
    if tool == "osascript":
        return _osascript(argv, args)
    for check in (_tests, _readers):
        if (v := check(argv, tool, args)) is not None:
            return v
    if tool == "mkdir" and (v := _mkdir(argv, args, where, folders, root)) is not None:
        return v
    if tool in FILE_WRITERS:
        if root is None or _outside(args, where, root):
            return _approval("high", f"{tool} {away if root is None else 'outside the project'}", argv)
        return Verdict("auto", f"{tool} inside the project", argv, sandboxed=True, risk="medium", sandbox="write",
                       root=str(root))
    if tool in ("uv", "npm", *RUNNERS):
        return _project_tool(argv, tool, args, where, folders, root, away)
    if tool in REACHES_OUT:
        return _approval("high", "reaches other machines or people", argv)
    if (tool, args[0] if args else "") in PUBLISH:
        return _approval("high", f"{tool} {args[0]} publishes, uninstalls or changes system settings", argv)
    return _approval("high", f"{tool} isn't known to be safe", argv)


def _tests(argv: list[str], tool: str, args: list[str]) -> Verdict | None:
    rest = (args if tool == "pytest" else argv[3:] if argv[:3] == ["uv", "run", "pytest"]
            or (tool in ("python", "python3") and argv[1:3] == ["-m", "pytest"]) else None)
    if rest is None:
        return None
    if (bad := _blocked_flag("pytest", rest)):
        return _approval("high", f"pytest flag {bad} isn't allowed automatically", argv)
    return Verdict("auto", "running the tests", [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *rest],
                   risk="low")


def _osascript(argv: list[str], args: list[str]) -> Verdict:
    """App scripting: a click can be fine; typing, sending, deleting never are."""
    script = " ".join(a for a in args if a != "-e")
    if any(not a.startswith("-") and Path(a).suffix in (".scpt", ".applescript", ".js") for a in args):
        return _approval("critical", "runs a script file", argv)
    if OSASCRIPT_DANGER.search(script):
        return _approval("critical", "this script can type, send, delete or change settings", argv)
    return _approval("high", "app scripting isn't allowed automatically", argv)


def _readers(argv: list[str], tool: str, args: list[str]) -> Verdict | None:
    """The read-only tools, which run anywhere in the no-writes, no-network sandbox."""
    if tool == "screen" and args[:1] in (["-ls"], ["-list"]) and len(args) <= 2:  # just lists sessions
        return Verdict("auto", "read-only screen listing", argv, risk="low")
    if tool in LOOKUPS:
        if (bad := _blocked_flag(tool, args)):
            return _approval("high", f"{tool} {bad} isn't allowed automatically", argv)
        # ps is setuid, which the sandbox can't launch; it only reads the process list and takes no files.
        return Verdict("auto", f"read-only {tool}", argv, sandboxed=tool != "ps", risk="low",
                       sandbox="read" if tool != "ps" else "")
    if tool == "find":
        if (bad := _blocked_flag("find", args)):
            return _approval("high", f"find {bad} can change or run things", argv)
        return Verdict("auto", "read-only find", argv, risk="low")
    if tool == "sed" and "-n" in args:  # printing lines; -i would edit in place
        if (bad := _blocked_flag("sed", args)):
            return _approval("high", f"sed {bad} edits files", argv)
        return Verdict("auto", "read-only sed", argv, risk="low")
    if tool in READERS:
        if (bad := _blocked_flag(tool, args)):
            return _approval("high", f"{tool} {bad} isn't allowed automatically", argv)
        safe = [tool, *GREP_EXCLUDES, *args] if tool == "grep" else argv
        return Verdict("auto", f"read-only {tool}", safe, risk="low")
    return None


def _mkdir(argv: list[str], args: list[str], where: Path, folders: Folders, root: Path | None) -> Verdict | None:
    """Making a folder can't hurt anything: fine anywhere in his home or temp (secrets were refused before), unless
    it is a project's own mkdir, which is graded as project work."""
    if root is not None and not _outside(args, where, root):
        return None
    targets = [full_path(a, where) for a in args if not a.startswith("-")]
    places = [Path.home().resolve(), Path("/private/tmp"), Path("/private/var/folders")]
    if not targets or not all(any(inside(t, p) for p in places) for t in targets) \
            or any(folders.is_read_only(t) for t in targets):
        return None
    parent = Path(os.path.commonpath([str(t.parent) for t in targets]))
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    return Verdict("auto", "making a folder", argv, sandboxed=True, risk="medium", sandbox="write", root=str(parent))


def _project_tool(argv: list[str], tool: str, args: list[str], where: Path, folders: Folders, root: Path | None,
                  away: str) -> Verdict:
    """uv, npm, python and make: ordinary project work, writing only inside the project."""
    if root is None:
        return _approval("high", f"{tool} {away}", argv)
    sub = args[0] if args else ""
    if tool == "uv" and sub == "run":
        inner = _uv_inner(args)
        if inner and inner[0] not in ("python", "python3", "pytest", "jarvis", "ruff", "mypy") \
                and not (where / inner[0]).is_file():
            return _grade(inner, Path(inner[0]).name, inner[1:], where, folders)  # judged like what it runs
    elif tool == "uv" and sub not in UV_OK:
        return _approval("high", f"uv {sub} isn't known to be safe", argv)
    if tool == "npm" and sub not in NPM_OK:
        return _approval("high", f"npm {sub} isn't known to be safe", argv)
    if tool in ("python", "python3") and (why := _outside([a for a in args[:1] if not a.startswith("-")], where, root)):
        return _approval("high", f"runs {why}, outside the project", argv)
    return Verdict("auto", f"{tool} in the project (writes stay inside it)", argv, sandboxed=True, risk="medium",
                   sandbox="write", root=str(root))


def _git(argv: list[str], args: list[str], where: Path, folders: Folders) -> Verdict:
    rest, git_dir, prefix = list(args), where, []
    while rest[:1] == ["-C"] and len(rest) >= 2:  # `-C <path>` only: -c, --git-dir and --work-tree stay refused
        git_dir = full_path(rest[1], git_dir)
        prefix += ["-C", str(git_dir)]
        rest = rest[2:]
    sub = rest[0] if rest else ""
    if sub.startswith("-"):
        return _approval("high", f"git option {sub} isn't allowed automatically (only -C)", argv)
    if _secret_arg([str(git_dir)], git_dir):
        return Verdict("reject", f"{git_dir} may hold secrets; never opened", [])
    if not git_dir.is_dir():
        return _approval("high", f"{git_dir} isn't a folder", argv)
    if (bad := _blocked_flag("git", rest)):
        return _approval("high", f"git flag {bad} can write or run programs", argv)
    flags = [a for a in rest[1:] if a.startswith("-")]
    listing = (sub == "branch" and all(a in GIT_BRANCH_LIST for a in rest[1:])
               or sub == "remote" and rest[1:] in ([], ["-v"], ["--verbose"])
               or sub == "tag" and rest[1:] in ([], ["-l"], ["--list"]) or sub == "stash" and rest[1:] == ["list"])
    if sub in GIT_READ or listing:
        extra = ["--no-ext-diff", "--no-textconv"] if sub in ("diff", "log", "show") else []
        safe = ["git", "--no-optional-locks", "-c", "core.pager=cat", "-c", "color.ui=false",
                "-c", "core.fsmonitor=false", *prefix, sub, *extra,
                *rest[1:]]
        return Verdict("auto", f"read-only git {sub}", safe, risk="low")
    if sub == "push" or (sub == "branch" and _flags(rest[1:]) & {"D", "--force"}):
        return _approval("critical" if _flags(rest[1:]) & {"f", "--force"} else "high",
                         f"git {sub} reaches out or deletes", argv)
    if sub in GIT_REFUSED:
        return _approval("high", f"git {sub} can lose work or change settings", argv)
    root = folders.writable_root(git_dir)
    if sub in GIT_WRITE and root is not None:
        if (why := _outside([a for a in rest[1:] if (git_dir / a).exists()], git_dir, root)):
            return _approval("high", f"{why} is outside the project", argv)
        return Verdict("auto", f"git {sub} in the project", argv, sandboxed=True, risk="medium", sandbox="git",
                       root=str(root))
    if sub in GIT_WRITE:
        return _approval("high", f"git {sub} outside your work folders or in a read-only folder", argv)
    return _approval("high", f"git {sub or flags} isn't known to be safe", argv)


def _open(argv: list[str], args: list[str], where: Path) -> Verdict:
    """`open -a App` and `open <file or folder>` run; a web address asks (it sends something out)."""
    has_target, i = False, 0
    while i < len(args):
        a = args[i]
        if a == "-a" and i + 1 < len(args):
            has_target, i = True, i + 2
            continue
        if a in OPEN_FLAGS:
            i += 1
            continue
        if a.startswith("-"):
            return _approval("high", f"open {a} isn't allowed automatically", argv)
        if "://" in a or a.startswith(("mailto:", "tel:", "sms:", "x-")):
            return _approval("high", "opens a web address or link", argv)
        full = full_path(a, where)
        if not full.exists():
            return _approval("high", f"{a} doesn't exist", argv)
        if full.suffix.lower() in OPEN_RISKY:
            return _approval("high", f"opening a {full.suffix} file can run it", argv)
        has_target, i = True, i + 1
    if not has_target:
        return _approval("high", "open needs an app or a file", argv)
    return Verdict("auto", "opening it on this Mac", argv, sandboxed=False, risk="low", sandbox="")


def _lsof(argv: list[str], args: list[str]) -> Verdict:
    i = 0
    while i < len(args):
        a = args[i]
        if a in LSOF_SIMPLE or (a.startswith("-") and not a.startswith("--") and len(a) > 2 and
                                all(f"-{c}" in LSOF_SIMPLE for c in a[1:])):  # -nP
            i += 1
        elif a[:2] in LSOF_VALUED and a.startswith("-"):
            i += 2 if len(a) == 2 and i + 1 < len(args) else 1
        else:
            return _approval("high", f"lsof {a} isn't allowed automatically", argv)
    # Like ps, it only reads kernel tables, and the sandbox can't launch it.
    return Verdict("auto", "read-only lsof", argv, sandboxed=False, risk="low", sandbox="")


def _rm(argv: list[str], args: list[str], where: Path, root: Path | None, away: str) -> Verdict:
    """Inside the project root, `rm` never runs: the handler moves what it names to the Trash, with an undo."""
    flags = _flags(args)
    paths = [a for a in args if not a.startswith("-")]
    if flags - {"r", "R", "f", "v", "d", "--recursive", "--force", "--verbose"} or not paths:
        return _approval("high", "rm with options that aren't just -r, -f or -v", argv)
    if root is None:
        return _approval("high", f"rm {away}", argv)
    fulls = [full_path(a, where) for a in paths]
    if any(not inside(f, root) or f == root or ".git" in f.relative_to(root).parts for f in fulls):
        return _approval("high", "rm outside the project, on the project folder itself, or inside .git", argv)
    return Verdict("trash", "moving it to the Trash", argv, risk="medium", sandbox="", root=str(root),
                   trash=tuple(fulls))
