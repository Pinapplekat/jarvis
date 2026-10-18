"""JARVIS applying an approved plan to its own code, with Elijah approving the result.

Off unless Elijah sets JARVIS_REPO himself. Flow: a plan Elijah approved (see plans.py)
is built by Claude Code in JARVIS's private clone, on its own branch. Tests must pass and
can't shrink. Elijah gets a summary + the patch with Deploy/Discard buttons. Deploy
merges into the clone's main, redeploys the background jobs from it, and syncs the
commit back to the project repo (fast-forward only, so nothing of Elijah's is
overwritten).

Builds and deploys run as separate detached processes: a build takes minutes, and a
deploy restarts the listener that would otherwise be running it. The builder ("jarvis-build")
runs Claude Code as a monitored session (jarvis.agent): sandboxed to the clone, every tool call
graded by the same hook as task mode. It reports to JARVIS, not to Elijah: progress is kept
for when he asks, notes and failures are passed on in JARVIS's words. A high-risk step (a
permission prompt) is decided by JARVIS when it's off Elijah's side of the line, and otherwise
forwarded to him as a plain-words question; it runs on the yes and the session resumes. A session that goes quiet for BUILD_HANG_MINUTES is
restarted from where it was; a builder that dies is restarted by the tick. `jarvis build
start|status|stop` controls it. Self-editing as a whole stays off until Elijah sets JARVIS_REPO.
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

from jarvis import config, reports
from jarvis.memory import Memory

CLONE = config.DATA_DIR / "src"
LOG = Path.home() / "Library/Logs/JARVIS/selfedit.log"
LOCK = config.DATA_DIR / "selfedit.lock"
BUILD_DIR = config.DATA_DIR / "build"     # the build session's state, log and status
STATUS = BUILD_DIR / "status.json"
PROGRESS_EVERY = 180                      # seconds between progress reports (kept for "how's it going?")
MAX_RESTARTS = 2                          # hung sessions, or a builder that died
GIT_ENV = {"GIT_AUTHOR_NAME": "JARVIS", "GIT_AUTHOR_EMAIL": "jarvis@localhost",
           "GIT_COMMITTER_NAME": "JARVIS", "GIT_COMMITTER_EMAIL": "jarvis@localhost"}
SAFETY_FILES = ["jarvis/approvals.py", "jarvis/chat.py", "jarvis/selfedit.py", "jarvis/telegram.py",
                "jarvis/web.py", "jarvis/mailrelay.py", "jarvis/schedule.py", "jarvis/phone.py"]
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

def read_status() -> dict:
    try:
        return json.loads(STATUS.read_text())
    except (OSError, ValueError):
        return {}


def _status(**fields) -> None:
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    data = {**read_status(), **fields}
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, STATUS)


def _alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # our own finished child lingers as a zombie until reaped
        done, _ = os.waitpid(int(pid), os.WNOHANG)
        return done == 0
    except ChildProcessError:
        return True


def builder_alive() -> bool:
    return _alive(read_status().get("pid"))


def spawn(command: str, edit_id: int | None = None) -> None:
    """Start `jarvis self-edit <command> [id]` in its own session so it outlives the caller.
    A build is only started if no builder is running (it would pick up the queue anyway)."""
    if command == "build" and builder_alive():
        return
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
    # Only the summary line matters; a huge test id elsewhere once made this search run for ever.
    m = re.search(r"(\d+) tests? collected", r.stdout[-2000:])
    return int(m[1]) if m else 0


def run_tests(cwd: Path) -> tuple[bool, str]:
    r = _run(TEST_CMD, cwd=cwd, timeout=900)
    tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-40:])
    return r.returncode == 0, tail


def run_agent(mem: Memory, tg, title: str, prompt: str, resume: str | None = None,
              runner=None, poll: float = 5, edit_id: int = 0, judge=None):
    """Run the build session to the end of its work: progress reported to JARVIS, high-risk
    steps decided by JARVIS or asked of Elijah, hangs restarted. Returns the agent.Outcome."""
    from jarvis import agent, reports
    runner = runner or agent.run_session
    rules = agent.AGENT_RULES.format(root=CLONE)
    progress = {"at": 0.0}

    def on_tool(update):
        _status(last_update=update[:140], last_output=time.time())
        if time.time() - progress["at"] >= PROGRESS_EVERY:
            progress["at"] = time.time()
            reports.add(mem, "build", edit_id, "progress", f"building {title}", f"now: {update[:140]}")

    restarts = 0
    while True:
        out = runner(prompt, CLONE, BUILD_DIR, config.SELF_EDIT_MODEL, rules, resume=resume, on_tool=on_tool,
                     on_tell=lambda text: reports.add(mem, "build", edit_id, "note", f"building {title}", text),
                     should_stop=lambda: bool(read_status().get("stop")),
                     hang_seconds=config.BUILD_HANG_MINUTES * 60, log_to=BUILD_DIR / "session.jsonl")
        resume = out.session_id or resume
        _status(session_id=resume, last_output=time.time())
        if out.stopped:
            raise RuntimeError("stopped by Elijah")
        if out.hung:
            restarts += 1
            _status(restarts=restarts)
            if restarts > MAX_RESTARTS:
                raise RuntimeError(f"the build session went quiet {restarts} times")
            _log(f"build session hung; resuming {resume}")
            prompt = "You went quiet for a long time and were restarted. Check what finished, then carry on."
            continue
        if out.blocked:
            outcome = _ask_and_wait(mem, tg, out.blocked, poll=poll, edit_id=edit_id, title=title, judge=judge)
            prompt = agent.resume_note(out.blocked, outcome)
            continue
        if out.is_error and not out.session_id:
            raise RuntimeError(f"Claude Code failed: {out.text.strip()[:300]}")
        return out


def _ask_and_wait(mem: Memory, tg, blocked: dict, poll: float = 5, edit_id: int = 0, title: str = "",
                  judge=None) -> str:
    """A paused step: JARVIS decides it when it's off Elijah's side of the line; otherwise ask
    him and wait for his answer (the listener runs it on his yes and records the output)."""
    from jarvis import agent, reports, terminal
    try:
        argv, cwd = agent.run_blocked(blocked)
    except ValueError as e:
        return f"(couldn't ask: {e})"
    goal = f"build the approved plan: {title}"
    approved, why = agent.review(judge, goal, blocked, CLONE)
    if approved:
        reports.add(mem, "build", edit_id, "progress", f"building {title}",
                    f"I approved `{blocked.get('command', '')[:150]}` myself ({why})")
        return agent.run_approved(blocked, CLONE)
    verdict = terminal.Verdict("approval", blocked.get("reason", ""), argv, sandboxed=False,
                               risk=blocked.get("risk") or "high", sandbox="")
    aid = terminal.request_approval(mem, verdict, cwd or str(CLONE), blocked.get("why") or "",
                                    extra={"build_id": edit_id or True})
    terminal.notify_commands(mem, tg)
    _status(phase="waiting for Elijah")
    deadline = time.time() + (config.COMMAND_APPROVAL_MINUTES + 2) * 60
    while time.time() < deadline:
        action = mem.get_action(aid)
        if action["status"] in ("done", "failed"):
            _status(phase="building")
            return mem.get_kv(f"agent_outcome:{aid}") or "(it ran; no output was recorded)"
        if action["status"] == "rejected":
            _status(phase="building")
            return "(Elijah rejected this command)"
        if read_status().get("stop"):
            raise RuntimeError("stopped by Elijah")
        time.sleep(poll)
    return "(the approval request expired before Elijah answered)"


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
        _status(pid=os.getpid(), started=time.time(), stop=False, phase="starting", restarts=0)
        try:
            while queued := mem.self_edits("queued"):
                _status(edit_id=queued[0]["id"], title=queued[0]["title"], phase="building", session_id=None)
                _build(mem, tg, queued[0]["id"])
        finally:
            _status(pid=None, phase="idle", ended=time.time())


def _judge(mem: Memory):
    """JARVIS's judgment for deciding paused build steps (Jev, or Haiku). None if unavailable."""
    try:
        from jarvis import judge
        return judge.engine(on_fallback=lambda e: mem.log("jev_fallback", str(e)[:200]))
    except Exception:
        return None


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
        judge = _judge(mem)
        out = run_agent(mem, tg, job["title"], prompt, edit_id=edit_id, judge=judge)
        passed, tail = run_tests(CLONE)
        if not passed:  # one more go, with the failures
            out = run_agent(mem, tg, job["title"], f"The tests fail:\n{tail}\nFix them, then give the summary again.",
                            resume=out.session_id, edit_id=edit_id, judge=judge)
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
        summary = (out.summary or out.text).strip()[:2000]
        if problem:
            _git("reset", "-q", "--hard")
            _git("clean", "-qfd")
            mem.set_self_edit(edit_id, status="failed", summary=f"{problem}\n\n{summary}"[:3000])
            reports.add(mem, "build", edit_id, "failed", f"building {job['title']}", f"couldn't finish: {problem[:1500]}")
            _log(f"build #{edit_id} failed: {problem[:200]}")
            return

        _git("add", "-A")
        _git("commit", "-q", "-m", f"Self-edit #{edit_id}: {job['title']}\n\n{job['request']}")
        stat = _git("show", "--stat", "--format=", "HEAD")
        patch = _git("format-patch", "-1", "--stdout", "HEAD")
        touched_safety = [f for f in files if f in SAFETY_FILES]

        mem.set_self_edit(edit_id, status="ready", summary=summary)
        text = f"🛠 {job['title']} is built and tests pass.\n\n{summary}\n\n{stat}"
        if touched_safety:
            text += f"\n\n⚠️ Touches safety-critical code: {', '.join(touched_safety)}. Worth a look at the patch."
        if note:
            text += f"\n\n({note})"
        tg.send_document(f"selfedit-{edit_id}.patch", patch.encode(), caption=f"{job['title']}: the full change")
        tg.send(text, buttons=[("Deploy ✅", f"se_deploy:{edit_id}"), ("Discard ✖️", f"se_discard:{edit_id}")])
        _log(f"build #{edit_id} ready ({len(files)} files)")
    except Exception as e:
        mem.set_self_edit(edit_id, status="failed", summary=str(e)[:3000])
        reports.add(mem, "build", edit_id, "failed", f"building {job['title']}", f"the build stopped ({str(e)[:300]})")
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
        tg.send(f"🚀 Deploying {job['title']}. Restarting, back in a few seconds."
                + (f"\n({sync})" if sync else ""))
        _log(f"deploy #{edit_id}: installing")
        # Reinstall from the clone's code. This restarts the listener (and replaces this
        # process's own code on disk), so it is the last thing we do.
        r = _run(["uv", "run", "--project", str(CLONE), "-q", "jarvis", "schedule", "install"],
                 cwd=CLONE, timeout=600)
        if r.returncode != 0:
            raise RuntimeError(f"install failed: {(r.stderr or r.stdout).strip()[-300:]}")
        reports.add(mem, "build", edit_id, "done", job["title"], "deployed and live")
        _log(f"deploy #{edit_id} done")
    except Exception as e:
        mem.set_self_edit(edit_id, status="failed", summary=str(e)[:3000])
        reports.add(mem, "build", edit_id, "failed", f"deploying {job['title']}", str(e)[:400])
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
        if not job["notified"]:  # the plan's button already said it's building; JARVIS reports back
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


def start(mem: Memory, tg) -> str:
    """`jarvis build start`: make sure a builder is running for anything queued (idempotent)."""
    if builder_alive():
        st = read_status()
        return f"already running: {st.get('title', '?')} ({st.get('phase', '?')}), pid {st.get('pid')}"
    if not mem.self_edits("queued"):
        return "nothing queued to build (approve a plan first)"
    spawn("build")
    return "started"


def stop_build(mem: Memory) -> str:
    """`jarvis build stop`: end the current build (its session is killed within seconds)."""
    st = read_status()
    if not builder_alive():
        return "no build is running"
    _status(stop=True)
    for _ in range(30):
        if not builder_alive():
            return "stopped"
        time.sleep(1)
    try:
        os.killpg(os.getpgid(int(st["pid"])), 15)
    except (ProcessLookupError, PermissionError):
        pass
    return "stopped (forced)"


def status_text() -> str:
    """`jarvis build status`: alive?, what it's on, last output, session id."""
    st = read_status()
    if not st:
        return "no build has run yet"
    alive = builder_alive()
    quiet = f"{int((time.time() - st['last_output']) // 60)} min ago" if st.get("last_output") else "-"
    return (f"builder: {'running, pid ' + str(st['pid']) if alive else 'not running'}\n"
            f"plan: {st.get('title', '-')} ({st.get('phase', '-') if alive else 'idle'})\n"
            f"last step: {st.get('last_update', '-')}, last output {quiet}\n"
            f"session: {st.get('session_id') or '-'}, restarts: {st.get('restarts', 0)}\n"
            f"session log: {BUILD_DIR / 'session.jsonl'}")


def check_health(mem: Memory, tg) -> str:
    """From the tick: a plan stuck 'building' with no builder alive gets restarted (twice at
    most, then it's marked failed and Elijah is told)."""
    stuck = mem.self_edits("building")
    if stuck and builder_alive():  # alive but silent (stuck before or inside the session): say so once
        st = read_status()
        quiet = time.time() - max(st.get("started") or 0, st.get("last_output") or 0)
        if quiet > config.BUILD_HANG_MINUTES * 60 and st.get("stall_reported") != st.get("started"):
            _status(stall_reported=st.get("started"))
            reports.add(mem, "build", stuck[0]["id"], "note", f"building {stuck[0]['title']}",
                        f"the build has shown no progress for {int(quiet // 60)} minutes ({st.get('phase', '?')}); "
                        "it may be stuck. `jarvis build stop` ends it, `jarvis build start` retries.")
            return "stalled (reported)"
        return "ok"
    if not stuck:
        return "ok"
    job = stuck[0]
    tries = int(mem.get_kv(f"build_restarts:{job['id']}") or 0)
    if tries >= MAX_RESTARTS:
        mem.set_self_edit(job["id"], status="failed", summary="the builder kept dying")
        reports.add(mem, "build", job["id"], "failed", f"building {job['title']}",
                    "the builder kept stopping, so I've set it aside; approving the plan again retries it")
        return "gave up"
    mem.set_kv(f"build_restarts:{job['id']}", str(tries + 1))
    mem.set_self_edit(job["id"], status="queued", notified=1)
    spawn("build")
    _log(f"builder for #{job['id']} was gone; restarted ({tries + 1})")
    return "restarted"


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
