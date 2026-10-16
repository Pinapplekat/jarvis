"""Terminal sense and terminal commands.

Sense: a zsh hook appends one line per command (time, exit code, folder, duration, command)
to a local log; JARVIS reads new lines into the timeline, redacted.

Commands: JARVIS can run commands it proposes in chat. Read-only ones on a strict allowlist,
inside the JARVIS project, run at once in a no-writes, no-network sandbox. Anything else is
sent to Telegram with Run/Reject buttons and only runs after Elijah taps Run. There's never a
shell (no pipes, redirects or substitution), output is redacted, and every command is logged.
"""

import json
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
READERS = {"ls", "cat", "head", "tail", "wc", "grep", "file", "stat", "du"}
LOOKUPS = {"which", "pgrep", "ps", "pwd", "date", "whoami", "uname", "sw_vers"}  # no file arguments
HELP_FLAGS = {"--help", "-h", "help", "--version", "-V", "version"}
GIT_READ = {"status", "log", "diff", "show"}
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
                 "--exclude-dir=.git", "--exclude-dir=.venv*", "--exclude-dir=__pycache__"]


@dataclass
class Verdict:
    kind: str              # auto | approval | reject
    reason: str
    argv: list[str]        # what will actually run (auto commands get safety flags added)
    sandboxed: bool = True  # auto commands run in the no-writes, no-network sandbox


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
    return argv


def _blocked_flag(tool: str, args: list[str]) -> str | None:
    for a in args:
        for flag in BLOCKED_FLAGS.get(tool, ()):
            if a == flag or (flag.startswith("--") and a.startswith(flag + "=")):
                return a
            # short flags can hide in a cluster: `tail -nf`, `grep -rf list`
            if len(flag) == 2 and a.startswith("-") and not a.startswith("--") and flag[1] in a[1:]:
                return a
    return None


def _check_paths(args: list[str], cwd: Path, root: Path) -> str | None:
    """Every non-flag argument that names a path must be inside the project and not secret."""
    from jarvis.introspect import Refused, resolve
    for a in args:
        if a.startswith("-"):
            continue
        full = a if Path(a).is_absolute() else str(cwd / a)
        try:
            resolve(full, root)
        except Refused as e:
            return str(e).replace(full, a)
    return None


def classify(command, cwd: Path | None = None) -> Verdict:
    """Decide how a proposed command may run. Anything not clearly read-only needs approval."""
    from jarvis.introspect import Refused, resolve, project_dir
    root = project_dir()
    try:
        argv = parse(command)
    except ValueError as e:
        return Verdict("reject", str(e), [])
    try:
        where = resolve(str(cwd), root) if cwd else root
    except Refused:
        return Verdict("approval", "runs outside the JARVIS project", argv)
    tool, args = Path(argv[0]).name, argv[1:]
    if argv[0] != tool:  # an explicit path to a program: let Elijah look at it
        return Verdict("approval", "runs a program by path", argv)

    if len(args) in (1, 2) and args[-1] in HELP_FLAGS and all(not a.startswith("/") for a in args):
        import shutil
        if shutil.which(tool):  # an installed tool explaining itself, sandboxed (no writes, no network)
            return Verdict("auto", f"{tool} help/version", argv)

    if tool == "git":
        if not args or args[0] not in GIT_READ:
            return Verdict("approval", f"git {args[0] if args else ''} isn't read-only".strip(), argv)
        if (bad := _blocked_flag("git", args)):
            return Verdict("approval", f"git flag {bad} can write or run programs", argv)
        from jarvis.introspect import is_secret
        if any(is_secret(Path(part)) for a in args[1:] for part in a.replace(":", "/").split("/") if part):
            return Verdict("approval", "names a file that may hold secrets", argv)
        paths = [a for a in args[1:] if a != "--" and not a.startswith("-") and (where / a).exists()]
        if (why := _check_paths(paths, where, root)):
            return Verdict("approval", why, argv)
        extra = ["--no-ext-diff", "--no-textconv"] if args[0] in ("diff", "log", "show") else []
        safe = ["git", "--no-optional-locks", "-c", "core.pager=cat", "-c", "color.ui=false",
                args[0], *extra, *args[1:]]
        return Verdict("auto", f"read-only git {args[0]}", safe)

    rest = (args if tool == "pytest" else argv[3:] if argv[:3] == ["uv", "run", "pytest"]
            or (tool in ("python", "python3") and argv[1:3] == ["-m", "pytest"]) else None)
    if rest is not None:
        if (bad := _blocked_flag("pytest", rest)):
            return Verdict("approval", f"pytest flag {bad} isn't allowed automatically", argv)
        if (why := _check_paths([a for a in rest if (where / a.split("::")[0]).exists()], where, root)):
            return Verdict("approval", why, argv)
        return Verdict("auto", "running the tests",
                       [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *rest])

    if tool in LOOKUPS:
        if (bad := _blocked_flag(tool, args)):
            return Verdict("approval", f"{tool} {bad} isn't allowed automatically", argv)
        # ps is setuid, which the sandbox can't launch; it only reads the process list and takes no files.
        return Verdict("auto", f"read-only {tool}", argv, sandboxed=tool != "ps")

    if tool == "find":
        if (bad := _blocked_flag("find", args)):
            return Verdict("approval", f"find {bad} can change or run things", argv)
        starts = [a for a in args if not a.startswith(("-", "(", ")", "!"))][:1]  # the folder to search
        if (why := _check_paths(starts or ["."], where, root)):
            return Verdict("approval", why, argv)
        return Verdict("auto", "read-only find", argv)

    if tool in READERS:
        if (bad := _blocked_flag(tool, args)):
            return Verdict("approval", f"{tool} {bad} isn't allowed automatically", argv)
        values = _flag_values(tool, args)
        paths = [a for a in args if not a.startswith("-") and a not in values]
        if tool == "grep" and paths and not any(a.startswith("-e") or a == "--regexp" for a in args):
            paths = paths[1:]  # the first plain argument is the pattern
        if (why := _check_paths(paths or ["."], where, root)):
            return Verdict("approval", why, argv)
        safe = [tool, *GREP_EXCLUDES, *args] if tool == "grep" else argv
        return Verdict("auto", f"read-only {tool}", safe)

    return Verdict("approval", f"{tool} isn't on the read-only list", argv)


def _flag_values(tool: str, args: list[str]) -> set[str]:
    """Arguments that are values of a flag (e.g. the 20 in `head -n 20`), not paths."""
    takes = {"head": {"-n", "-c"}, "tail": {"-n", "-c"}, "grep": {"-e", "--regexp", "-m", "-A", "-B", "-C",
                                                                   "--max-count", "--include", "--exclude"}}
    out, flags = set(), takes.get(tool, set())
    for a, nxt in zip(args, args[1:]):
        if a in flags:
            out.add(nxt)
    return out


def audit(entry: dict, path: Path | None = None) -> None:
    path = path or AUDIT
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry}
    with path.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def run_command(verdict: Verdict, cwd: Path | None = None, approved_by: str = "auto"):
    """Run a classified command (auto: sandboxed; approved: as Elijah, still no shell)."""
    from jarvis.introspect import project_dir, run_argv
    where = Path(cwd).expanduser() if cwd else project_dir()
    auto = approved_by == "auto"
    if auto and verdict.kind != "auto":
        raise ValueError("this command needs approval")
    timeout = TEST_TIMEOUT if "pytest" in verdict.argv[:4] else AUTO_TIMEOUT if auto else APPROVED_TIMEOUT
    result = run_argv(verdict.argv, where, timeout=timeout, sandboxed=auto and verdict.sandboxed)
    audit({"argv": verdict.argv, "cwd": str(where), "class": verdict.kind, "reason": verdict.reason,
           "approved_by": approved_by, "exit": result.code, "timed_out": result.timed_out,
           "output_chars": len(result.output)})
    return result


def describe(result) -> str:
    status = "timed out" if result.timed_out else ("couldn't run" if result.code is None else f"exit {result.code}")
    return f"[{status}]\n{result.output}"


# ----- approval over Telegram -----
def request_approval(mem: Memory, verdict: Verdict, cwd: str | None, why: str, extra: dict | None = None) -> int:
    """Queue a command for Elijah's Run/Reject. `extra` can link it to a task and carry a risk label."""
    from jarvis.introspect import project_dir
    payload = {"argv": verdict.argv, "cwd": str(Path(cwd).expanduser()) if cwd else str(project_dir()),
               "why": why[:300], "reason": verdict.reason, **(extra or {})}
    aid = mem.queue_action("run_command", payload)
    audit({"argv": verdict.argv, "cwd": payload["cwd"], "class": "approval", "reason": verdict.reason,
           "approved_by": None, "pending_id": aid})
    return aid


def format_request(action_id: int, payload: dict) -> str:
    task = f" for task #{payload['task_id']}" if payload.get("task_id") else ""
    risk = f"\n{payload['risk']}" if payload.get("risk") else ""
    return (f"Command #{action_id}{task} needs your OK ({payload.get('reason', 'not read-only')}){risk}\n\n"
            f"$ {shlex.join(payload['argv'])}\nin {payload['cwd']}"
            + (f"\n\nWhy: {payload['why']}" if payload.get("why") else ""))


def notify_commands(mem: Memory, tg) -> int:
    count = 0
    for action in mem.unnotified_actions():
        if action["kind"] != "run_command":
            continue
        aid = action["id"]
        tg.send(format_request(aid, json.loads(action["payload"])),
                buttons=[("Run ▶️", f"cmd_run:{aid}"), ("Reject ✖️", f"cmd_reject:{aid}")])
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
    """Run/Reject tapped in Elijah's chat. Returns the toast text. `on_done(mem, payload,
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
        return f"Rejected #{action_id}"
    if verb != "cmd_run":
        return "Unknown button"
    mem.resolve_action(action_id, "approved")  # before running, so a second tap can't run it twice

    db_file = mem.db.execute("PRAGMA database_list").fetchone()[2]  # '' for an in-memory db

    def go():
        # SQLite connections can't cross threads, so a background run opens its own.
        own = Memory(db_file) if background and db_file else mem
        try:
            verdict = Verdict("approval", payload.get("reason", ""), payload["argv"])
            result = run_command(verdict, payload["cwd"], approved_by="telegram")
            if not payload.get("task_id"):  # a task shows it in its progress message instead
                tg.send(f"Command #{action_id}: $ {shlex.join(payload['argv'])}\n{describe(result)}"[:3900])
            own.resolve_action(action_id, "done" if result.code == 0 else "failed")
            own.log("command", f"#{action_id} ran: {'timed out' if result.timed_out else f'exit {result.code}'}")
            outcome = describe(result)
        except Exception as e:  # never leave him waiting in silence
            own.resolve_action(action_id, "failed")
            own.log("command", f"#{action_id} failed: {str(e)[:200]}")
            tg.send(f"Command #{action_id} failed: {e}"[:3900])
            outcome = f"(couldn't run it: {e})"
        try:
            on_done(own, payload, action_id, outcome)
        finally:
            if own is not mem:
                own.db.close()

    threading.Thread(target=go, daemon=True).start() if background else go()
    return f"Running #{action_id}"


def recent_audit(n: int = 20, path: Path | None = None) -> list[dict]:
    path = path or AUDIT
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()[-n:] if line.strip()]
