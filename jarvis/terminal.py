"""Terminal sense and terminal commands.

Sense: a zsh hook appends one line per command (time, exit code, folder, duration, command)
to a local log; JARVIS reads new lines into the timeline, redacted.

Commands: JARVIS can run commands it proposes in chat or in a task, each graded by risk.
Low (reading, anywhere on the Mac) runs at once in a no-writes, no-network sandbox. Medium
(ordinary work inside the project: edits, git commits, tests, installs) runs at once in a
sandbox that can only write inside the project. High and critical (deletes elsewhere, system
changes, sudo, credentials, pushing, anything that reaches other people, anything unknown)
wait for Elijah's yes, typed, spoken or tapped. Secret files and folders are refused outright
whatever the grade, and the sandboxes can't read them either. There's never a shell (no
pipes, redirects or substitution), output is redacted, and every command is logged.
"""

import json
import re
import shlex
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jarvis import config
from jarvis.memory import Memory
from jarvis.repos import known_repos, project_for
from jarvis.screen import redact

LOG = config.DATA_DIR / "terminal.log"
HOOK = config.DATA_DIR / "jarvis-hook.zsh"
OFFSET_KEY = "terminal_offset"
SOURCE_LINE = f'[[ -f "{HOOK}" ]] && source "{HOOK}"  # JARVIS terminal sense'

HOOK_SCRIPT = f"""# JARVIS terminal sense: logs each command's time, exit code, folder, duration.
# Remove the matching 'source' line from ~/.zshrc to turn it off.
zmodload zsh/datetime
_jarvis_log="{LOG}"
_jarvis_preexec() {{ _jarvis_cmd="$1"; _jarvis_start=$EPOCHREALTIME }}
_jarvis_precmd() {{
  local code=$?
  [[ -z "$_jarvis_cmd" ]] && return
  local dur=$(( EPOCHREALTIME - _jarvis_start ))
  printf '%s\\t%s\\t%s\\t%.1f\\t%s\\n' "$EPOCHSECONDS" "$code" "$PWD" "$dur" "${{_jarvis_cmd//$'\\n'/ }}" >> "$_jarvis_log"
  unset _jarvis_cmd
}}
autoload -Uz add-zsh-hook
add-zsh-hook preexec _jarvis_preexec
add-zsh-hook precmd _jarvis_precmd
"""


def install_hook(zshrc: Path = Path.home() / ".zshrc") -> str:
    """Write the hook script and add one 'source' line to ~/.zshrc (idempotent)."""
    HOOK.parent.mkdir(parents=True, exist_ok=True)
    HOOK.write_text(HOOK_SCRIPT)
    existing = zshrc.read_text() if zshrc.exists() else ""
    if SOURCE_LINE in existing:
        return "already installed"
    with zshrc.open("a") as f:
        f.write(("\n" if existing and not existing.endswith("\n") else "") + SOURCE_LINE + "\n")
    return f"added to {zshrc}; open a new terminal tab to start logging"


def hook_installed(zshrc: Path = Path.home() / ".zshrc") -> bool:
    return zshrc.exists() and SOURCE_LINE in zshrc.read_text()


def ingest(mem: Memory, log: Path = LOG) -> int:
    """Read new terminal log lines into the timeline. Returns how many were added."""
    if not log.exists():
        return 0
    offset = int(mem.get_kv(OFFSET_KEY) or 0)
    size = log.stat().st_size
    if size < offset:
        offset = 0  # log was rotated/cleared
    with log.open("rb") as f:
        f.seek(offset)
        data = f.read()
    complete = data[: data.rfind(b"\n") + 1]  # leave a half-written last line for next time
    repos = known_repos(mem)
    added = 0
    for raw in complete.decode(errors="replace").splitlines():
        parts = raw.split("\t", 4)
        if len(parts) != 5:
            continue
        epoch, code, cwd, dur, cmd = parts
        try:
            at = datetime.fromtimestamp(int(epoch), timezone.utc)
        except ValueError:
            continue
        status = "ok" if code == "0" else f"exit {code}"
        mem.observe("terminal", f"$ {redact(cmd)[:300]}  [{status}, {dur}s]", app="terminal",
                    title=cwd, project=project_for(repos, cwd=cwd), at=at)
        added += 1
    mem.set_kv(OFFSET_KEY, str(offset + len(complete)))
    return added


# ----- running commands -----
AUDIT = config.DATA_DIR / "commands.jsonl"
SHELL_CHARS = set("|&;<>`$\n")  # there's no shell, so these could only mislead
AUTO_TIMEOUT, TEST_TIMEOUT, APPROVED_TIMEOUT = 30, 120, 120
RISKS = ("low", "medium", "high", "critical")
# Low: read-only, anywhere on the Mac (secrets excepted), in the no-writes, no-network sandbox.
READERS = {"ls", "cat", "head", "tail", "wc", "grep", "file", "stat", "du", "cd", "diff", "realpath",
           "shasum", "tree"}
LOOKUPS = {"which", "pgrep", "ps", "pwd", "date", "whoami", "uname", "sw_vers", "echo"}  # no file arguments
HELP_FLAGS = {"--help", "-h", "help", "--version", "-V", "version"}
GIT_READ = {"status", "log", "diff", "show", "blame", "ls-files", "rev-parse"}
# Medium: ordinary work inside the project (or a task's folder), in a sandbox that can only
# write there. git may also write .git (not its hooks or config); the rest can't touch .git.
FILE_WRITERS = {"mkdir", "touch", "cp", "mv", "rm"}
GIT_WRITE = {"add", "commit", "checkout", "switch", "restore", "stash", "branch", "mv", "rm", "tag",
             "fetch", "pull", "merge", "init"}
RUNNERS = {"python", "python3", "make"}
UV_OK = {"sync", "add", "remove", "lock", "run", "tree"}
NPM_OK = {"install", "ci", "test", "run"}
# Critical: system-wide, credentials, disks, or things that reach other people.
CRITICAL = {"sudo", "su", "doas", "diskutil", "dd", "mkfs", "newfs_apfs", "fdisk", "shutdown", "reboot",
            "halt", "launchctl", "csrutil", "spctl", "security", "tccutil", "nvram", "pmset", "systemsetup",
            "networksetup", "dscl", "passwd", "mail", "sendmail", "chown", "kextload",
            "softwareupdate", "scutil"}
# Flags that make an otherwise read-only command write, run programs, follow forever or reach out.
# (git's global options like -c or --git-dir can't appear: the subcommand must come first.)
BLOCKED_FLAGS = {
    "git": ("--output", "--ext-diff", "--textconv", "--exec", "--upload-pack", "--receive-pack",
            "--open-files-in-pager"),
    "tail": ("-f", "-F", "--follow", "--retry"),
    "grep": ("-f", "--file", "--exclude-from", "--include-from"),
    "find": ("-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"),
    "date": ("-s", "--set"),
    "pytest": ("-p", "-c", "--rootdir", "--basetemp", "--confcutdir", "--pdb", "--trace", "--pyargs",
               "--override-ini", "-o"),
}
GREP_EXCLUDES = ["--exclude=.env*", "--exclude=*token*", "--exclude=*secret*", "--exclude=*.db",
                 "--exclude=*credential*", "--exclude=id_rsa*", "--exclude=id_ed25519*", "--exclude=*.pem",
                 "--exclude=*.key", "--exclude=*.keychain*", "--exclude-dir=.git", "--exclude-dir=.venv*",
                 "--exclude-dir=__pycache__", "--exclude-dir=.ssh", "--exclude-dir=.gnupg", "--exclude-dir=.aws",
                 "--exclude-dir=Keychains", "--exclude-dir=Cookies", "--exclude-dir=Messages",
                 f"--exclude-dir={config.BROWSER_PROFILE.name}"]


@dataclass
class Verdict:
    kind: str              # auto | approval | reject
    reason: str
    argv: list[str]        # what will actually run (auto commands get safety flags added)
    sandboxed: bool = True  # auto commands run in a sandbox (see `sandbox`)
    risk: str = "high"     # low | medium | high | critical
    sandbox: str = "read"  # read (no writes, no network) | write | git (writes only inside `root`)
    root: str = ""         # the folder a write/git sandbox may write to
    yours: str = ""        # why only Elijah may approve it (his side of the line), or "" if JARVIS may


def parse(command) -> list[str]:
    """A command string or argv list -> argv. Shell syntax is refused, never interpreted."""
    if isinstance(command, list):
        argv = [str(a) for a in command]
    else:
        text = str(command).strip()
        if any(c in text for c in SHELL_CHARS):
            raise ValueError("shell syntax (pipes, redirects, $, ;, &&) isn't supported; one plain command only")
        argv = shlex.split(text)
    if not argv or not argv[0].strip():
        raise ValueError("empty command")
    if any("\0" in a or "\n" in a for a in argv):
        raise ValueError("bad characters in command")
    import os
    return [os.path.expanduser(a) if a == "~" or a.startswith("~/") else a for a in argv]  # no shell to do it


def _blocked_flag(tool: str, args: list[str]) -> str | None:
    for a in args:
        for flag in BLOCKED_FLAGS.get(tool, ()):
            if a == flag or (flag.startswith("--") and a.startswith(flag + "=")):
                return a
            # short flags can hide in a cluster: `tail -nf`, `grep -rf list`
            if len(flag) == 2 and a.startswith("-") and not a.startswith("--") and flag[1] in a[1:]:
                return a
    return None


def _full(arg: str, cwd: Path) -> Path:
    import os
    raw = Path(os.path.expanduser(arg))
    return (raw if raw.is_absolute() else cwd / raw).resolve()  # follows symlinks, collapses ..


def _secret_arg(args: list[str], cwd: Path) -> str | None:
    """The first argument naming something secret (by name, folder, or where a symlink leads).
    Plain words that aren't paths (a grep pattern like TOKEN) don't count."""
    from jarvis.introspect import is_secret, is_secret_path
    for a in args:
        if a.startswith("-") and "=" not in a:
            continue
        value = a.partition("=")[2] if a.startswith("-") else a
        if not value:
            continue
        full = _full(value, cwd)
        looks_like_path = "/" in value or ":" in value or value.startswith(("~", ".")) or full.exists()
        if looks_like_path and (is_secret(Path(value.replace(":", "/"))) or is_secret_path(full)):
            return a
    return None


UV_VALUE_FLAGS = {"--extra", "--with", "--project", "--python", "--group", "--directory", "--package",
                  "--env-file", "--with-requirements", "-p"}


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


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _outside(args: list[str], cwd: Path, root: Path) -> str | None:
    """The first path argument that lands outside `root` or inside its .git, or None."""
    for a in args:
        if a.startswith("-"):
            continue
        full = _full(a, cwd)
        if not _inside(full, root) or ".git" in full.relative_to(root).parts:
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


# Elijah's side of the line: JARVIS never approves these itself, however sure it is.
GIT_YOURS = {"push", "reset", "clean", "rebase", "filter-branch", "gc", "prune", "config", "remote"}
REACHES_OUT = {"ssh", "scp", "rsync", "sftp", "ftp", "gh", "twine", "nc", "telnet", "mail", "sendmail"}
PUBLISH = {("npm", "publish"), ("uv", "publish"), ("brew", "uninstall"), ("brew", "remove"),
           ("defaults", "write"), ("defaults", "delete"), ("crontab", "-r"), ("crontab", "-e")}
OSASCRIPT_DANGER = re.compile(
    r"do shell script|keystroke|key code|\bMessages\b|\bMail\b|\bsend\b|delete|\bFinder\b|Terminal|"
    r"System (?:Settings|Preferences|Events.+(?:shut down|restart|log out|sleep))|administrator|password|"
    r"empty (?:the )?trash|\bmove\b|duplicate|make new|\bquit\b|run script|open location|URL|keychain|"
    r"clipboard|the clipboard|set the clipboard", re.I)
CURL_SENDS = ("-d", "--data", "--data-raw", "--data-binary", "--data-urlencode", "-F", "--form", "-T",
              "--upload-file", "-X", "--request", "--json")


def elijahs_call(argv: list[str], risk: str) -> str:
    """Why this needs Elijah himself, or "" if JARVIS may decide: anything critical (sudo, system,
    credentials, disks), irreversible (deleting, rewriting git history), or reaching other people
    or the outside world with his data (pushing, publishing, posting, messaging)."""
    tool, args = Path(argv[0]).name, argv[1:]
    sub = args[0] if args else ""
    if risk == "critical":
        return "critical"
    if tool in ("rm", "mv") or (tool == "find" and "-delete" in args):
        return "deletes or overwrites files"
    if tool == "git" and sub in GIT_YOURS or (tool == "git" and sub == "branch" and _flags(args) & {"D", "d"}):
        return f"git {sub} can't be undone or reaches out"
    if tool in REACHES_OUT:
        return "reaches other machines or people"
    if (tool, sub) in PUBLISH:
        return f"{tool} {sub} publishes, uninstalls or changes system settings"
    if tool in ("curl", "wget") and any(a == f or a.startswith(f + "=") for a in args for f in CURL_SENDS):
        return "sends data out"
    if tool == "uv" and sub == "run":
        inner = _uv_inner(args)
        return elijahs_call(inner, risk) if inner else ""
    return ""


def classify(command, cwd: Path | None = None, root: Path | None = None) -> Verdict:
    """Grade a proposed command low, medium, high or critical. Low (reading, anywhere but
    secrets) and medium (ordinary work inside `root`, the project by default) run on their own,
    each in a sandbox; high and critical wait for a yes. Anything not recognised is high. A high
    one off Elijah's side of the line (`yours` empty) may be approved by JARVIS itself; the rest
    only by Elijah. Naming a secret file or folder is refused outright, whatever the grade."""
    v = _grade(command, cwd, root)
    if v.kind == "approval":
        v.yours = elijahs_call(v.argv, v.risk)
    return v


def _grade(command, cwd: Path | None = None, root: Path | None = None) -> Verdict:
    from jarvis.introspect import project_dir
    root = Path(root or project_dir()).resolve()
    try:
        argv = parse(command)
    except ValueError as e:
        return Verdict("reject", str(e), [])
    where = _full(str(cwd), root) if cwd else root
    tool, args = Path(argv[0]).name, argv[1:]
    if (bad := _secret_arg(argv, where)) or _secret_arg([str(where)], where):
        return Verdict("reject", f"{bad or 'that folder'} may hold secrets; never opened", [])
    if argv[0] != tool:  # an explicit path to a program: let Elijah look at it
        return _approval("high", "runs a program by path", argv)
    in_root = _inside(where, root)
    medium = dict(sandboxed=True, risk="medium", sandbox="write", root=str(root))

    if tool in CRITICAL:
        return _approval("critical", f"{tool} changes the system, credentials or reaches other people", argv)
    if tool in ("rm", "chmod", "mv") and _flags(args) & {"r", "R", "--recursive"} and (
            not in_root or any(not a.startswith("-") and not _inside(_full(a, where), root) for a in args)):
        return _approval("critical", f"recursive {tool} outside the project", argv)

    if len(args) in (1, 2) and args[-1] in HELP_FLAGS and all(not a.startswith("/") for a in args):
        import shutil
        if shutil.which(tool):  # an installed tool explaining itself, sandboxed (no writes, no network)
            return Verdict("auto", f"{tool} help/version", argv, risk="low")

    if tool == "git":
        sub = args[0] if args else ""
        if (bad := _blocked_flag("git", args)):
            return _approval("high", f"git flag {bad} can write or run programs", argv)
        if sub in GIT_READ:
            extra = ["--no-ext-diff", "--no-textconv"] if sub in ("diff", "log", "show") else []
            safe = ["git", "--no-optional-locks", "-c", "core.pager=cat", "-c", "color.ui=false",
                    sub, *extra, *args[1:]]
            return Verdict("auto", f"read-only git {sub}", safe, risk="low")
        if sub == "push" or (sub == "branch" and _flags(args) & {"D", "--force"}):
            return _approval("critical" if _flags(args) & {"f", "--force"} else "high", f"git {sub} reaches out or deletes", argv)
        if sub in ("reset", "clean", "rebase", "config", "remote", "filter-branch", "gc", "prune"):
            return _approval("high", f"git {sub} can lose work or change settings", argv)
        if sub in GIT_WRITE and in_root:
            if (why := _outside([a for a in args[1:] if (where / a).exists()], where, root)):
                return _approval("high", f"{why} is outside the project", argv)
            return Verdict("auto", f"git {sub} in the project", argv, **{**medium, "sandbox": "git"})
        return _approval("high", f"git {sub} outside the project" if sub in GIT_WRITE else f"git {sub} isn't known to be safe", argv)

    rest = (args if tool == "pytest" else argv[3:] if argv[:3] == ["uv", "run", "pytest"]
            or (tool in ("python", "python3") and argv[1:3] == ["-m", "pytest"]) else None)
    if rest is not None:
        if (bad := _blocked_flag("pytest", rest)):
            return _approval("high", f"pytest flag {bad} isn't allowed automatically", argv)
        return Verdict("auto", "running the tests",
                       [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *rest], risk="low")

    if tool == "screencapture":  # his own screen, saved to a temp or screenshot folder: low
        from jarvis.attachments import SHOTS_DIR
        targets = [a for a in args if not a.startswith("-")]
        safe = [Path("/private/tmp"), Path("/private/var/folders"), SHOTS_DIR.resolve(), root]
        if targets and all(any(_inside(_full(t, where), d) for d in safe) for t in targets) \
                and not {"-P", "-u", "-B"} & set(args):
            # Unsandboxed: it needs Screen Recording, and it only writes the image file.
            return Verdict("auto", "a screenshot to a temp folder", argv, sandboxed=False, risk="low", sandbox="")
        return _approval("high", "screenshot to an unusual place", argv)

    if tool == "osascript":  # app scripting: a click can be fine; typing, sending, deleting never are
        script = " ".join(a for a in args if a != "-e")
        if any(not a.startswith("-") and Path(a).suffix in (".scpt", ".applescript", ".js") for a in args):
            return _approval("critical", "runs a script file", argv)
        if OSASCRIPT_DANGER.search(script):
            return _approval("critical", "this script can type, send, delete or change settings", argv)
        return _approval("high", "app scripting (JARVIS judges it)", argv)

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

    if tool in READERS:
        if (bad := _blocked_flag(tool, args)):
            return _approval("high", f"{tool} {bad} isn't allowed automatically", argv)
        safe = [tool, *GREP_EXCLUDES, *args] if tool == "grep" else argv
        return Verdict("auto", f"read-only {tool}", safe, risk="low")

    if tool in FILE_WRITERS:
        if not in_root or (why := _outside(args, where, root)):
            return _approval("high", f"{tool} outside the project", argv)
        if tool == "rm" and _flags(args) & {"r", "R", "--recursive"}:
            return _approval("high", "recursive delete", argv)
        return Verdict("auto", f"{tool} inside the project", argv, **medium)

    if tool in ("uv", "npm", *RUNNERS):
        if not in_root:
            return _approval("high", f"{tool} outside the project", argv)
        sub = args[0] if args else ""
        if tool == "uv" and sub == "run":
            inner = _uv_inner(args)
            if inner and inner[0] not in ("python", "python3", "pytest", "jarvis", "ruff", "mypy") \
                    and not (where / inner[0]).is_file():
                return _grade(inner, where, root)  # judged like the command it runs
        elif tool == "uv" and sub not in UV_OK:
            return _approval("high", f"uv {sub} isn't known to be safe", argv)
        if tool == "npm" and sub not in NPM_OK:
            return _approval("high", f"npm {sub} isn't known to be safe", argv)
        if tool in ("python", "python3") and (why := _outside([a for a in args[:1] if not a.startswith("-")], where, root)):
            return _approval("high", f"runs {why}, outside the project", argv)
        return Verdict("auto", f"{tool} in the project (writes stay inside it)", argv, **medium)

    return _approval("high", f"{tool} isn't known to be safe", argv)


def audit(entry: dict, path: Path | None = None) -> None:
    path = path or AUDIT
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry}
    with path.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def run_command(verdict: Verdict, cwd: Path | None = None, approved_by: str = "auto"):
    """Run a classified command (auto: in its sandbox; approved: as Elijah, still no shell)."""
    from jarvis.introspect import SANDBOX, project_dir, run_argv, write_sandbox
    where = Path(cwd).expanduser() if cwd else project_dir()
    auto = approved_by == "auto"
    if auto and verdict.kind != "auto":
        raise ValueError("this command needs approval")
    profile = None
    if auto and verdict.sandboxed:
        profile = (write_sandbox(Path(verdict.root or project_dir()), git=verdict.sandbox == "git")
                   if verdict.sandbox in ("write", "git") else SANDBOX)
    slow = "pytest" in verdict.argv[:4] or verdict.risk == "medium"
    timeout = TEST_TIMEOUT if slow else AUTO_TIMEOUT if auto else APPROVED_TIMEOUT
    result = run_argv(verdict.argv, where, timeout=timeout, sandboxed=False, profile=profile)
    audit({"argv": verdict.argv, "cwd": str(where), "class": verdict.kind, "risk": verdict.risk,
           "reason": verdict.reason, "approved_by": approved_by, "exit": result.code,
           "timed_out": result.timed_out, "output_chars": len(result.output)})
    return result


def describe(result) -> str:
    status = "timed out" if result.timed_out else ("couldn't run" if result.code is None else f"exit {result.code}")
    return f"[{status}]\n{result.output}"


# ----- approval over Telegram -----
def request_approval(mem: Memory, verdict: Verdict, cwd: str | None, why: str, extra: dict | None = None) -> int:
    """Queue a command for Elijah's yes (typed, spoken or the Run button). `extra` can link it
    to a task or a build, or say {"job": True} to run it as a background job once approved."""
    from jarvis.introspect import project_dir
    payload = {"argv": verdict.argv, "cwd": str(Path(cwd).expanduser()) if cwd else str(project_dir()),
               "why": why[:300], "reason": verdict.reason, "level": verdict.risk, **(extra or {})}
    aid = mem.queue_action("run_command", payload)
    audit({"argv": verdict.argv, "cwd": payload["cwd"], "class": "approval", "risk": verdict.risk,
           "reason": verdict.reason, "approved_by": None, "pending_id": aid})
    return aid


def describe_action(payload: dict) -> str:
    """A command in plain words, for confirmations: 'git push origin main in JARVIS'."""
    cmd = shlex.join(payload["argv"])
    return f"{cmd[:120]}{'…' if len(cmd) > 120 else ''} in {Path(payload['cwd']).name or payload['cwd']}"


def format_request(action_id: int, payload: dict) -> str:
    """The plain-words question. No internal numbers: he answers yes or no to this one thing."""
    level = payload.get("level") or "high"
    flag = "⚠️ " if level == "critical" else ""
    who = " for the task I'm working on" if payload.get("task_id") else " for the build" if payload.get("build_id") else ""
    job = "\nIt runs in the background (no time limit); I'll ping you when it ends." if payload.get("job") else ""
    return (f"{flag}Before I go ahead{who}, sir: I'd like to run this ({level} risk: "
            f"{payload.get('reason', 'not read-only')}).\n\n$ {shlex.join(payload['argv'])}\nin {payload['cwd']}"
            + (f"\n\nWhy: {payload['why']}" if payload.get("why") else "") + job
            + "\n\nIs that what you meant? Reply yes to run it, no to cancel (or use the buttons).")


def notify_commands(mem: Memory, tg, say=None) -> int:
    """Send each new request. `say(text)` (optional) asks out loud too, when he's at the Mac."""
    count = 0
    for action in mem.unnotified_actions():
        if action["kind"] != "run_command":
            continue
        aid = action["id"]
        payload = json.loads(action["payload"])
        tg.send(format_request(aid, payload),
                buttons=[("Run ▶️", f"cmd_run:{aid}"), ("Reject ✖️", f"cmd_reject:{aid}")])
        if say:
            say(f"Sir, before I go ahead I need your OK on a {payload.get('level') or 'high'} risk command. "
                "It's in Telegram; is that what you meant?")
        mem.mark_notified(aid)
        count += 1
    return count


def _expired(action, now: datetime) -> bool:
    created = datetime.fromisoformat(action["created_at"])
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return now - created > timedelta(minutes=config.COMMAND_APPROVAL_MINUTES)


def expire_commands(mem: Memory, now: datetime | None = None, on_expired=None) -> int:
    """Unanswered requests lapse, so an old one can never be run by a late tap.
    `on_expired(action_id, payload)` lets a task that was waiting on it wrap up."""
    now = now or datetime.now(timezone.utc)
    stale = [a for a in mem.pending_actions(kind="run_command") if _expired(a, now)]
    for a in stale:
        payload = json.loads(a["payload"])
        mem.resolve_action(a["id"], "rejected")
        audit({"argv": payload["argv"], "class": "approval", "approved_by": None,
               "pending_id": a["id"], "outcome": "expired"})
        if on_expired:
            on_expired(a["id"], payload)
    return len(stale)


def handle_button(mem: Memory, tg, verb: str, action_id: int, background: bool = True,
                  now: datetime | None = None, on_done=None) -> str:
    """Run/Reject tapped (or a yes/no typed or said) in Elijah's chat. Returns the toast text. `on_done(mem, payload,
    action_id, result_text)` gets the outcome (so a task waiting on this command can carry on);
    `mem` is a connection usable on the calling thread."""
    on_done = on_done or (lambda db, payload, aid, text: None)
    action = mem.get_action(action_id)
    if not action or action["kind"] != "run_command" or action["status"] != "pending":
        return "Already handled"
    payload = json.loads(action["payload"])
    if _expired(action, now or datetime.now(timezone.utc)):
        mem.resolve_action(action_id, "rejected")
        audit({"argv": payload["argv"], "class": "approval", "pending_id": action_id, "outcome": "expired"})
        on_done(mem, payload, action_id, "(the approval request expired before Elijah answered)")
        return "Expired; ask again"
    if verb == "cmd_reject":
        mem.resolve_action(action_id, "rejected")
        audit({"argv": payload["argv"], "class": "approval", "approved_by": "rejected", "pending_id": action_id})
        mem.log("command", f"#{action_id} rejected")
        on_done(mem, payload, action_id, "(Elijah rejected this command; find another way or finish and explain)")
        return "Rejected"
    if verb != "cmd_run":
        return "Unknown button"
    mem.resolve_action(action_id, "approved")  # before running, so a second tap can't run it twice

    db_file = mem.db.execute("PRAGMA database_list").fetchone()[2]  # '' for an in-memory db

    def go():
        # SQLite connections can't cross threads, so a background run opens its own.
        own = Memory(db_file) if background and db_file else mem
        try:
            if payload.get("job"):
                from jarvis import jobs
                job_id = jobs.start_job(payload["argv"], payload["cwd"], action_id, payload.get("why", ""))
                audit({"argv": payload["argv"], "cwd": payload["cwd"], "class": "approval",
                       "approved_by": "telegram", "pending_id": action_id, "outcome": f"job {job_id}"})
                own.resolve_action(action_id, "done")
                own.log("command", f"#{action_id} started as job #{job_id}")
                outcome = f"(started as background job #{job_id}; check it with job_status)"
                if not payload.get("task_id") and not payload.get("build_id"):
                    tg.send(f"Running it in the background: $ {shlex.join(payload['argv'])}\n"
                            "I'll ping you when it finishes, fails or stalls.")
            else:
                verdict = Verdict("approval", payload.get("reason", ""), payload["argv"])
                result = run_command(verdict, payload["cwd"], approved_by="telegram")
                if not payload.get("task_id") and not payload.get("build_id"):  # those report it themselves
                    tg.send(f"$ {shlex.join(payload['argv'])}\n{describe(result)}"[:3900])
                own.resolve_action(action_id, "done" if result.code == 0 else "failed")
                own.log("command", f"#{action_id} ran: {'timed out' if result.timed_out else f'exit {result.code}'}")
                outcome = describe(result)
        except Exception as e:  # never leave him waiting in silence
            own.resolve_action(action_id, "failed")
            own.log("command", f"#{action_id} failed: {str(e)[:200]}")
            tg.send(f"That command couldn't run: {e}"[:3900])
            outcome = f"(couldn't run it: {e})"
        try:
            on_done(own, payload, action_id, outcome)
        finally:
            if own is not mem:
                own.db.close()

    threading.Thread(target=go, daemon=True).start() if background else go()
    return "Running it"


def recent_audit(n: int = 20, path: Path | None = None) -> list[dict]:
    path = path or AUDIT
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()[-n:] if line.strip()]
