"""JARVIS applying an approved plan to its own code, with Elijah approving the result.

Off unless Elijah sets JARVIS_REPO himself. Flow: a plan Elijah approved (see plans.py)
is built by Claude Code in JARVIS's private clone, on its own branch. Tests must pass and
can't shrink. Elijah gets a summary + the patch with Deploy/Discard buttons. Deploy
merges into the clone's main, redeploys the background jobs from it, and syncs the
commit back to the project repo (fast-forward only, so nothing of Elijah's is
overwritten).

Builds and deploys run as separate detached processes: a build takes minutes, and a
deploy restarts the listener that would otherwise be running it.
"""

import fcntl
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from jarvis import config
from jarvis.memory import Memory

CLONE = config.DATA_DIR / "src"
LOG = Path.home() / "Library/Logs/JARVIS/selfedit.log"
LOCK = config.DATA_DIR / "selfedit.lock"
GIT_ENV = {"GIT_AUTHOR_NAME": "JARVIS", "GIT_AUTHOR_EMAIL": "jarvis@localhost",
           "GIT_COMMITTER_NAME": "JARVIS", "GIT_COMMITTER_EMAIL": "jarvis@localhost"}
SAFETY_FILES = ["jarvis/approvals.py", "jarvis/chat.py", "jarvis/selfedit.py", "jarvis/telegram.py",
                "jarvis/web.py", "jarvis/mailrelay.py", "jarvis/schedule.py"]
FORBIDDEN = re.compile(r"(^|/)\.env($|\.(?!example$))")  # .env, .env.local... but not .env.example
TEST_CMD = ["uv", "run", "--extra", "dev", "pytest", "-q"]

AGENT_PROMPT = """You are improving JARVIS, Elijah's personal assistant, in this repository
(Python, managed with uv). README.md explains the architecture.

Change #{id}: {title}
{request}

The plan Elijah approved:
{plan}

How to work:
- Make the smallest change that does this well, in the existing style. No unrelated refactors.
- Add or update tests in tests/. Run them with exactly: uv run --extra dev pytest -q
  Keep going until they all pass. Don't delete or weaken existing tests.
- Update README.md if behavior or commands change.
- Never read or edit .env or any secrets. Don't commit; JARVIS commits for you.
- Keep JARVIS's safety rules intact: emails only go out after Elijah approves in Telegram;
  only Elijah's Telegram chat is obeyed; the chat model runs with no tools and only proposes
  validated actions; web page content is untrusted; self-edits need Elijah's approval before
  deploy. If the request would break one of these, don't do that part and say so.

When finished, reply with a short plain-text summary for Elijah: 2-5 casual sentences on
what changed, how to use it, and any caveats. No markdown."""

def _log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 600, retries: int = 1,
         env: dict | None = None) -> subprocess.CompletedProcess:
    """Run a command; retry briefly on iCloud's intermittent 'Resource deadlock avoided'."""
    full_env = {**os.environ, **(env or {})}
    for attempt in range(retries):
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=full_env)
        if r.returncode == 0 or "deadlock" not in (r.stderr + r.stdout).lower() or attempt == retries - 1:
            return r
        time.sleep(3)
    return r


def _git(*args: str, cwd: Path = CLONE, check: bool = True, retries: int = 1) -> str:
    r = _run(["git", *args], cwd=cwd, retries=retries, env=GIT_ENV)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout.strip()


def enabled() -> bool:
    return bool(config.JARVIS_REPO) and (Path(config.JARVIS_REPO) / ".git").exists()


def branch(edit_id: int) -> str:
    return f"selfedit/{edit_id}"


# ---------- spawning detached workers ----------

def spawn(command: str, edit_id: int | None = None) -> None:
    """Start `jarvis self-edit <command> [id]` in its own session so it outlives the caller."""
    project = Path(__file__).resolve().parent.parent
    args = ["uv", "run", "--project", str(project), "-q", "jarvis", "self-edit", command]
    if edit_id is not None:
        args.append(str(edit_id))
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as out:
        subprocess.Popen(args, stdout=out, stderr=out, stdin=subprocess.DEVNULL,
                         start_new_session=True, cwd=project)


# ---------- clone management ----------

def prepare_clone() -> str:
    """Make CLONE's main match the project repo where possible. Returns a note for the user."""
    if not (CLONE / ".git").exists():
        CLONE.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "clone", "-q", config.JARVIS_REPO, str(CLONE)], retries=3, timeout=300)
        if not (CLONE / ".git").exists():
            raise RuntimeError("couldn't clone the project repo")
    # The clone is JARVIS's own working copy: leftovers from a crashed build can go.
    _git("reset", "-q", "--hard")
    _git("clean", "-qfd")
    _git("checkout", "-q", "main")
    note = ""
    if _run(["git", "fetch", "-q", "origin"], cwd=CLONE, retries=3, timeout=120).returncode != 0:
        note = "couldn't fetch the latest from your project repo; built on my last copy"
    elif _run(["git", "merge", "-q", "--ff-only", "origin/main"], cwd=CLONE, env=GIT_ENV).returncode != 0:
        note = "my copy and your project repo have diverged; built on my copy"
    return note


def count_tests(cwd: Path) -> int:
    r = _run([*TEST_CMD, "--collect-only"], cwd=cwd, timeout=300)
    m = re.search(r"(\d+) tests? collected", r.stdout)
    return int(m[1]) if m else 0


def run_tests(cwd: Path) -> tuple[bool, str]:
    r = _run(TEST_CMD, cwd=cwd, timeout=900)
    tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-40:])
    return r.returncode == 0, tail


def run_agent(prompt: str, resume: str | None = None) -> dict:
    from jarvis.chat import find_claude
    cmd = [find_claude(), "-p", "--model", config.SELF_EDIT_MODEL,
           "--tools", "Read,Edit,Write,Glob,Grep,Bash",
           "--allowedTools", "Read", "Edit", "Write", "Glob", "Grep",
           "Bash(uv run --extra dev pytest:*)", "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)",
           "--permission-mode", "acceptEdits", "--strict-mcp-config", "--setting-sources", "",
           "--output-format", "json"]
    if resume:
        cmd += ["--resume", resume]
    r = _run(cmd + ["--", prompt], cwd=CLONE, timeout=1800)
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"Claude Code failed: {(r.stderr or r.stdout).strip()[:300]}") from None


def changed_files() -> list[str]:
    out = _git("status", "--porcelain", "--untracked-files=all")
    return [line[3:].split(" -> ")[-1] for line in out.splitlines() if line.strip()]


# ---------- build ----------

def build_queued(mem: Memory, tg) -> None:
    """Build every queued self-edit, one at a time. Only one builder runs at once."""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return  # the running builder will pick up the queue
        while queued := mem.self_edits("queued"):
            _build(mem, tg, queued[0]["id"])


def _build(mem: Memory, tg, edit_id: int) -> None:
    job = mem.get_self_edit(edit_id)
    if not mem.set_self_edit(edit_id, expect=("queued",), status="building"):
        return
    _log(f"build #{edit_id} start: {job['title']}")
    try:
        note = prepare_clone()
        _git("branch", "-q", "-D", branch(edit_id), check=False)
        _git("checkout", "-q", "-b", branch(edit_id))
        _run(["uv", "sync", "-q", "--extra", "dev"], cwd=CLONE, timeout=600)
        baseline = count_tests(CLONE)

        prompt = AGENT_PROMPT.format(id=edit_id, title=job["title"], request=job["request"],
                                     plan=job["plan"] or "(none)")
        out = run_agent(prompt)
        passed, tail = run_tests(CLONE)
        if not passed:  # one more go, with the failures
            out = run_agent(f"The tests fail:\n{tail}\nFix them, then give the summary again.",
                            resume=out.get("session_id"))
            passed, tail = run_tests(CLONE)

        files = changed_files()
        problem = None
        if not files:
            problem = "no code changes were made"
        elif bad := [f for f in files if FORBIDDEN.search(f)]:
            problem = f"it touched secrets files ({', '.join(bad)})"
        elif not passed:
            problem = "tests still fail:\n" + tail[-1200:]
        elif count_tests(CLONE) < baseline:
            problem = "the number of tests went down"
        summary = str(out.get("result") or "").strip()[:2000]
        if problem:
            _git("reset", "-q", "--hard")
            _git("clean", "-qfd")
            mem.set_self_edit(edit_id, status="failed", summary=f"{problem}\n\n{summary}"[:3000])
            tg.send(f"🛠 #{edit_id} {job['title']}: couldn't finish, {problem[:1500]}")
            _log(f"build #{edit_id} failed: {problem[:200]}")
            return

        _git("add", "-A")
        _git("commit", "-q", "-m", f"Self-edit #{edit_id}: {job['title']}\n\n{job['request']}")
        stat = _git("show", "--stat", "--format=", "HEAD")
        patch = _git("format-patch", "-1", "--stdout", "HEAD")
        touched_safety = [f for f in files if f in SAFETY_FILES]

        mem.set_self_edit(edit_id, status="ready", summary=summary)
        text = f"🛠 #{edit_id} {job['title']} is built and tests pass.\n\n{summary}\n\n{stat}"
        if touched_safety:
            text += f"\n\n⚠️ Touches safety-critical code: {', '.join(touched_safety)}. Worth a look at the patch."
        if note:
            text += f"\n\n({note})"
        tg.send_document(f"selfedit-{edit_id}.patch", patch.encode(), caption=f"#{edit_id} full change")
        tg.send(text, buttons=[("Deploy ✅", f"se_deploy:{edit_id}"), ("Discard ✖️", f"se_discard:{edit_id}")])
        _log(f"build #{edit_id} ready ({len(files)} files)")
    except Exception as e:
        mem.set_self_edit(edit_id, status="failed", summary=str(e)[:3000])
        tg.send(f"🛠 #{edit_id} {job['title']}: build crashed ({str(e)[:300]})")
        _log(f"build #{edit_id} crashed: {e}")


# ---------- deploy / discard ----------

def deploy(mem: Memory, tg, edit_id: int) -> None:
    job = mem.get_self_edit(edit_id)
    if not mem.set_self_edit(edit_id, expect=("ready",), status="deploying"):
        return
    _log(f"deploy #{edit_id} start")
    try:
        prepare_clone()
        before = _git("rev-parse", "HEAD")
        if _run(["git", "merge", "-q", "--no-edit", branch(edit_id)], cwd=CLONE, env=GIT_ENV).returncode != 0:
            _git("merge", "--abort", check=False)
            raise RuntimeError("it conflicts with newer changes; ask me to redo it")
        if _git("rev-parse", "HEAD^{tree}") != _git("rev-parse", f"{branch(edit_id)}^{{tree}}"):
            passed, tail = run_tests(CLONE)  # merged with something new: re-check
            if not passed:
                _git("reset", "-q", "--hard", before)
                raise RuntimeError("tests fail after merging with newer changes; ask me to redo it")

        from jarvis.schedule import PROJECT
        app_env = PROJECT / ".env"
        if app_env.exists():
            (CLONE / ".env").write_bytes(app_env.read_bytes())
            (CLONE / ".env").chmod(0o600)

        mem.set_self_edit(edit_id, status="deployed")
        sync = sync_back()
        tg.send(f"🚀 Deploying #{edit_id} {job['title']}. Restarting, back in a few seconds."
                + (f"\n({sync})" if sync else ""))
        _log(f"deploy #{edit_id}: installing")
        # Reinstall from the clone's code. This restarts the listener (and replaces this
        # process's own code on disk), so it is the last thing we do.
        r = _run(["uv", "run", "--project", str(CLONE), "-q", "jarvis", "schedule", "install"],
                 cwd=CLONE, timeout=600)
        if r.returncode != 0:
            raise RuntimeError(f"install failed: {(r.stderr or r.stdout).strip()[-300:]}")
        tg.send(f"✅ #{edit_id} is live.")
        _log(f"deploy #{edit_id} done")
    except Exception as e:
        mem.set_self_edit(edit_id, status="failed", summary=str(e)[:3000])
        tg.send(f"⚠️ Couldn't deploy #{edit_id}: {str(e)[:400]}")
        _log(f"deploy #{edit_id} failed: {e}")


def sync_back() -> str:
    """Fast-forward the project repo to the clone's main. Returns a note if it couldn't."""
    repo = Path(config.JARVIS_REPO)
    current = _run(["git", "branch", "--show-current"], cwd=repo, retries=3).stdout.strip()
    if current != "main":
        return f"didn't sync to your project folder: it's on branch {current or '?'}, not main"
    r = _run(["git", "pull", "-q", "--ff-only", str(CLONE), "main"], cwd=repo, retries=3, timeout=120)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip().splitlines()
        return "didn't sync to your project folder (" + (msg[-1][:200] if msg else "git error") + \
               f"). To get it: git pull --ff-only \"{CLONE}\" main"
    return ""


def discard(mem: Memory, edit_id: int) -> bool:
    if not mem.set_self_edit(edit_id, expect=("ready", "failed"), status="discarded"):
        return False
    if (CLONE / ".git").exists():
        _git("checkout", "-q", "main", check=False)
        _git("branch", "-q", "-D", branch(edit_id), check=False)
    return True


# ---------- Telegram side ----------

def queue_approved(mem: Memory, tg, edit_id: int) -> str:
    """Called when Elijah approves a plan and self-editing is on. Returns the toast text."""
    if not mem.set_self_edit(edit_id, expect=("approved",), status="queued", notified=0):
        return "Already handled"
    start_queued(mem, tg)
    return "Approved, building it"


def start_queued(mem: Memory, tg) -> None:
    """Announce newly queued requests and make sure a builder is running."""
    queued = mem.self_edits("queued")
    for job in queued:
        if not job["notified"]:
            tg.send(f"🛠 On it: #{job['id']} {job['title']}. I'll text you when it's ready to review (usually 5-15 min).")
            mem.set_self_edit(job["id"], notified=1)
    if queued:
        spawn("build")


def handle_button(mem: Memory, tg, verb: str, edit_id: int) -> str:
    """Returns the short toast text for the button press."""
    if not enabled():
        return "Self-editing isn't set up"
    if verb == "se_deploy":
        job = mem.get_self_edit(edit_id)
        if not job or job["status"] != "ready":
            return f"Can't deploy (it's {job['status'] if job else 'missing'})"
        spawn("deploy", edit_id)
        return "Deploying"
    if verb == "se_discard":
        return "Discarded" if discard(mem, edit_id) else "Already handled"
    return "Unknown button"


def main(argv: list[str]) -> None:
    """Entry for the detached workers: build | deploy <id>."""
    from jarvis.telegram import TelegramClient
    mem, tg = Memory(config.DB_PATH), TelegramClient()
    if argv[0] == "build":
        build_queued(mem, tg)
    elif argv[0] == "deploy":
        deploy(mem, tg, int(argv[1]))
    else:
        sys.exit(f"unknown self-edit command {argv[0]}")
