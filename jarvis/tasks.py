"""Tasks: JARVIS working a multi-step job on its own, the way Claude Code does.

Each task is a Claude Code session (jarvis.agent) confined to the task's folder: it reads and
edits code, runs commands and tests, checks the results and keeps going until the goal is met.
Reads go anywhere but secrets; edits and ordinary commands stay inside the folder, in a sandbox;
a high-risk step pauses the task and asks Elijah in plain words, and JARVIS runs it for the
session on his yes, then resumes it. With no folder given, a task works in the JARVIS project
read-only: changing JARVIS's own code still takes an approved improvement plan.

Tasks report to JARVIS, not to Elijah (jarvis.reports): progress is kept for when he asks how
it's going, and JARVIS tells him when it's done, failed, or the session has something worth
passing on (that's a step, not the end). A paused high-risk step goes to JARVIS first: JARVIS
approves it itself when it's off Elijah's side of the line and clearly fits the goal
(agent.review), and only asks Elijah otherwise. "stop" ends a task at once. Budgets: TASK_MAX_TURNS per run, TASK_MAX_MINUTES of working time, MAX_RUNS runs.
A fast judge (Jev or Haiku) double-checks a "done" before it's reported. The session id is
stored, so a task cut off by a restart resumes where it was.
"""

import json
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from jarvis import config, reports
from jarvis.memory import Memory

MAX_RUNS = 8                 # session runs per task (each approval or restart starts another)
PROGRESS_EVERY = 10          # steps between progress reports (for "how's it going?", never sent)
ACTIVE = ("queued", "running", "waiting")
STOP_WORDS = re.compile(r"^\s*(?:stop|cancel|abort)(?:\s+(?:the\s+)?task)?(?:\s*#?(?P<id>\d+))?\s*[.!]*\s*$", re.I)
TASKS_DIR = config.DATA_DIR / "tasks"
NO_EDIT_NOTE = ("This is JARVIS's own project and it's read-only to you: changing JARVIS's code takes an "
                "improvement plan Elijah approves. If the goal needs a code change, finish with STATUS: stuck "
                "and say a plan is needed.")

Runner = Callable[..., object]  # jarvis.agent.run_session


# ----- storage -----
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def check_folder(folder: str | None) -> Path:
    """The folder a task may work in. The JARVIS project (read-only) when none is given."""
    from jarvis.introspect import is_secret_path
    if not folder:
        return config.PROJECT_DIR.resolve()
    path = Path(folder).expanduser().resolve()
    home = Path.home().resolve()
    if not path.is_dir():
        raise ValueError(f"no folder {folder}")
    if path in (Path("/"), home) or home.is_relative_to(path) or is_secret_path(path):
        raise ValueError(f"{folder} is too broad or private for a task; pick a project folder")
    return path


def create(mem: Memory, goal: str, folder: str | None = None) -> int:
    goal = goal.strip()[:1000]
    if not goal:
        raise ValueError("a task needs a goal")
    root = check_folder(folder)
    cur = mem.db.execute("INSERT INTO tasks (goal, folder, created_at, updated_at) VALUES (?, ?, ?, ?)",
                         (goal, str(root), _now(), _now()))
    mem.db.commit()
    mem.log("task", f"#{cur.lastrowid} queued")
    return cur.lastrowid


def get(mem: Memory, task_id: int) -> sqlite3.Row | None:
    return mem.db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


def recent(mem: Memory, n: int = 5) -> list[sqlite3.Row]:
    return mem.db.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (n,)).fetchall()


def _save(mem: Memory, task_id: int, **fields) -> None:
    if "steps" in fields:
        fields["steps"] = json.dumps(fields["steps"][-60:])
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    mem.db.execute(f"UPDATE tasks SET {cols} WHERE id = ?", (*fields.values(), task_id))
    mem.db.commit()


def steps_of(task) -> list[dict]:
    return json.loads(task["steps"] or "[]")


def db_file(mem: Memory) -> str:
    """The database file behind `mem`, so a worker thread can open its own connection."""
    return mem.db.execute("PRAGMA database_list").fetchone()[2]


def folder_of(task) -> Path:
    return Path(task["folder"] or config.PROJECT_DIR).resolve()


def editable(root: Path) -> bool:
    """JARVIS's own project is read-only to tasks."""
    return root != config.PROJECT_DIR.resolve()


def state_dir(task_id: int) -> Path:
    return TASKS_DIR / str(int(task_id))


# ----- what chat sees -----
def for_prompt(mem: Memory) -> str:
    lines = []
    for t in recent(mem, 5):
        steps = steps_of(t)
        last = steps[-1]["update"] if steps else "not started"
        tail = f" → {t['summary'][:200]}" if t["summary"] else f" (last: {last[:120]})"
        where = "" if not editable(folder_of(t)) else f" in {folder_of(t)}"
        lines.append(f"- task #{t['id']} [{t['status']}, {len(steps)} steps, {int(t['active_seconds'] // 60)} min]"
                     f"{where} {t['goal'][:120]}{tail}")
        if t["status"] in ACTIVE and steps:  # enough to say where it's at without a step list
            lines.append("  recent steps: " + "; ".join(x["update"][:80] for x in steps[-3:]))
    for a in mem.db.execute("SELECT * FROM pending_actions WHERE kind = 'run_command' ORDER BY id DESC LIMIT 6"):
        p = json.loads(a["payload"])
        state = {"pending": "waiting for his yes" if a["notified_at"] else "about to be sent for OK",
                 "approved": "approved, running", "done": "ran OK", "failed": "ran, failed",
                 "rejected": "rejected or expired"}.get(a["status"], a["status"])
        if p.get("job") and a["status"] == "done":
            state = "started as a background job"
        lines.append(f"- command #{a['id']} [{state}] $ {' '.join(p['argv'])[:120]}")
    return "\n".join(lines) or "(none)"


def first_prompt(t, root: Path) -> str:
    parts = [f"Now: {datetime.now():%A %B %-d, %Y %-I:%M %p}. Your folder: {root}.",
             f"JARVIS's own project: {config.PROJECT_DIR}. JARVIS's logs: {Path.home() / 'Library/Logs/JARVIS'} "
             "(tick, listen, ears, watch, menubar, phone .log).",
             f"Elijah's goal (from his chat):\n<goal>\n{t['goal']}\n</goal>",
             "Work until it's done and verified."]
    if not editable(root):
        parts.insert(2, NO_EDIT_NOTE)
    return "\n\n".join(parts)


# ----- the loop -----
class Worker:
    """Runs tasks. Each worker thread gets its own DB connection (SQLite needs that)."""

    def __init__(self, tg, runner: Runner | None = None, judge=None, log=print, say=None,
                 hang_seconds: float = 300):
        self.tg, self.log, self.judge, self.say = tg, log, judge, say
        self.hang_seconds = hang_seconds
        if runner is None:
            from jarvis.agent import run_session
            runner = run_session
        self.runner = runner

    def run(self, mem: Memory, task_id: int) -> None:
        try:
            self._loop(mem, task_id)
        except Exception as e:  # never leave a task silently "running"
            self.log(f"task #{task_id} crashed: {e}")
            self._end(mem, task_id, "failed", f"I hit an internal error on that task, sir ({str(e)[:200]}).")

    def _loop(self, mem: Memory, task_id: int) -> None:
        from jarvis import agent
        checked = False
        attached: list[str] = []  # images the session wants him to see, sent with the final report
        while True:
            t = get(mem, task_id)
            if not t or t["status"] != "running":
                return  # stopped (or finished) from elsewhere
            if t["runs"] >= MAX_RUNS or t["active_seconds"] >= config.TASK_MAX_MINUTES * 60:
                why = (f"{t['runs']} runs" if t["runs"] >= MAX_RUNS else f"{int(t['active_seconds'] // 60)} minutes")
                return self._end(mem, task_id, "failed", self._wrap_up(t, f"ran out of budget ({why})"))
            root = folder_of(t)
            prompt = t["resume_note"] or (first_prompt(t, root) if not t["session_id"] else "Carry on with the goal.")
            steps = steps_of(t)

            def on_tool(update, steps=steps, goal=t["goal"]):
                steps.append({"n": len(steps) + 1, "update": update[:140], "ok": None})
                if steps[:-1] and steps[-2].get("ok") is None:
                    steps[-2]["ok"] = True  # it moved on, so the last one ran
                _save(mem, task_id, steps=steps)
                if len(steps) % PROGRESS_EVERY == 0:
                    reports.add(mem, "task", task_id, "progress", goal[:80], f"{len(steps)} steps in; now: {update[:120]}")

            def on_tell(text, goal=t["goal"]):
                reports.add(mem, "task", task_id, "note", goal[:80], text)

            started = time.time()
            rules = agent.AGENT_RULES.format(root=root)
            out = self.runner(prompt, root, state_dir(task_id), config.TASK_MODEL, rules,
                              resume=t["session_id"], max_turns=config.TASK_MAX_TURNS, on_tool=on_tool,
                              on_tell=on_tell, edits=editable(root),
                              should_stop=lambda: (get(mem, task_id) or {"status": "gone"})["status"] != "running",
                              hang_seconds=self.hang_seconds, log_to=state_dir(task_id) / "session.jsonl")
            attached += [p for p in out.attachments if p not in attached]
            if steps and steps[-1].get("ok") is None and not out.blocked:
                steps[-1]["ok"] = not out.is_error
            _save(mem, task_id, steps=steps, session_id=out.session_id or t["session_id"], runs=t["runs"] + 1,
                  resume_note=None, active_seconds=t["active_seconds"] + time.time() - started)
            if out.stopped:
                return
            if out.blocked:
                return self._ask(mem, task_id, out.blocked)
            if out.hung:
                _save(mem, task_id, resume_note="You went quiet for a long time and were restarted. Check what "
                                                "finished, then carry on.")
                continue
            if out.status == "done":
                if not checked and (doubt := self._doubt(t["goal"], steps, out.summary)):
                    checked = True
                    _save(mem, task_id, resume_note=f"A checker isn't convinced the goal is met ({doubt}). Verify "
                                                    "it properly, then finish again.")
                    continue
                return self._end(mem, task_id, "done", out.summary or "Done, sir.", attached)
            if out.status == "stuck":
                return self._end(mem, task_id, "failed", out.summary or "I couldn't finish that, sir.", attached)
            if out.is_error and not out.session_id:
                return self._end(mem, task_id, "failed",
                                 f"I couldn't start working on that, sir ({out.text.strip()[:200] or 'Claude Code failed'}).")
            _save(mem, task_id, resume_note="You stopped before finishing (the step limit for one run). Keep "
                                            "going towards the goal; end with the STATUS line when done.")

    def _ask(self, mem: Memory, task_id: int, blocked: dict) -> None:
        """A paused high-risk step. JARVIS decides first; only Elijah's side of the line (or a step
        JARVIS isn't sure about) goes to him, and then the task waits for his answer."""
        from jarvis import agent, terminal
        t = get(mem, task_id)
        try:
            argv, cwd = agent.run_blocked(blocked)
        except ValueError as e:
            _save(mem, task_id, resume_note=f"That step can't be approved ({e}). Use one plain command, or "
                                            "another way.")
            return self._loop(mem, task_id)
        approved, why = agent.review(self.judge, t["goal"], blocked, folder_of(t))
        if approved:
            outcome = agent.run_approved(blocked, folder_of(t))
            steps = steps_of(t)
            steps.append({"n": len(steps) + 1, "update": f"Approved myself: {blocked.get('command', '')[:100]}",
                          "ok": not outcome.startswith("[couldn't")})
            reports.add(mem, "task", task_id, "progress", t["goal"][:80],
                        f"I approved `{blocked.get('command', '')[:150]}` myself ({why})")
            mem.log("self_approved", f"task #{task_id}: {blocked.get('command', '')[:200]}")
            _save(mem, task_id, steps=steps, resume_note=agent.resume_note(blocked, outcome).replace(
                "Elijah said yes, and JARVIS ran", "JARVIS approved it and ran"))
            return self._loop(mem, task_id)
        verdict = terminal.Verdict("approval", blocked.get("reason", ""), argv, sandboxed=False,
                                   risk=blocked.get("risk") or "high", sandbox="")
        aid = terminal.request_approval(mem, verdict, cwd or str(folder_of(get(mem, task_id))),
                                        blocked.get("why") or "", extra={"task_id": task_id})
        _save(mem, task_id, status="waiting", waiting_on=aid, blocked=json.dumps(blocked))
        terminal.notify_commands(mem, self.tg, say=self.say)  # JARVIS's question, right away

    def _doubt(self, goal: str, steps: list[dict], summary: str) -> str | None:
        """The fast judge's second opinion on a 'done'. A reason if it's clearly not met, else None."""
        if self.judge is None:
            return None
        from jarvis.judge import Question
        recent_steps = "\n".join(f"{s['n']}. {s['update']}" for s in steps[-12:])
        try:
            a = self.judge(f"Task goal: {goal}\n\nSteps (newest last):\n{recent_steps}\n\nAgent's summary: {summary}",
                           [Question("goal_met", "Has the task goal been fully achieved and verified?")])["goal_met"]
        except Exception:
            return None
        return "it doesn't look verified yet" if not a.value and a.confidence >= 0.7 else None

    def _wrap_up(self, t, why: str) -> str:
        tried = "; ".join(s["update"] for s in steps_of(t)[-4:]) or "nothing yet"
        return f"I stopped that task, sir: {why}. Last things I tried: {tried}."

    def _end(self, mem: Memory, task_id: int, status: str, summary: str, attachments: list[str] = ()) -> None:
        t = get(mem, task_id)
        if not t or t["status"] in ("done", "failed", "stopped"):
            return
        _save(mem, task_id, status=status, summary=summary[:2000], waiting_on=None, resume_note=None)
        mem.log("task", f"#{task_id} {status} after {len(steps_of(t))} steps")
        on_own = [s["update"].removeprefix("Approved myself: ") for s in steps_of(t)
                  if s["update"].startswith("Approved myself: ")]
        told = summary + (f" (Ran on my own along the way: {'; '.join(on_own[:5])}.)" if on_own else "")
        reports.add(mem, "task", task_id, "done" if status == "done" else "failed", t["goal"][:80], told,
                    attachments=attachments)


def progress_text(t) -> str:
    steps = steps_of(t)
    marks = {True: "✓", False: "✗"}
    lines = [f"{marks.get(s.get('ok'), '…')} {s['update']}" for s in steps[-10:]]
    if len(steps) > 10:
        lines.insert(0, f"… {len(steps) - 10} earlier steps")
    state = {"queued": "starting…", "running": "working…", "waiting": "⏸ waiting for your yes",
             "done": "✅ done", "failed": "❌ stopped without finishing", "stopped": "⏹ stopped"}[t["status"]]
    return f"Task: {t['goal'][:200]}\n\n" + ("\n".join(lines) + "\n\n" if lines else "") + state


# ----- running tasks in the background (inside `jarvis listen`) -----
_lock = threading.Lock()
_busy: set[int] = set()


def _in_thread(path: str, worker: Worker, task_id: int, fn) -> None:
    def go():
        mem = Memory(path)
        try:
            fn(mem)
        finally:
            with _lock:
                _busy.discard(task_id)
            mem.db.close()
    threading.Thread(target=go, daemon=True, name=f"task-{task_id}").start()


def start_queued(mem: Memory, worker: Worker, background: bool = True) -> list[int]:
    """Start every queued task (new, resumed after Elijah's answer, or after a restart)."""
    if not config.TASKS:
        return []
    started = []
    for t in mem.db.execute("SELECT id FROM tasks WHERE status = 'queued' ORDER BY id").fetchall():
        with _lock:
            if t["id"] in _busy:
                continue
            _busy.add(t["id"])
        _save(mem, t["id"], status="running")
        started.append(t["id"])
        if background:
            _in_thread(db_file(mem), worker, t["id"], lambda m, i=t["id"]: worker.run(m, i))
        else:
            worker.run(mem, t["id"])
            with _lock:
                _busy.discard(t["id"])
    return started


def requeue(mem: Memory, payload: dict, action_id: int, result: str) -> bool:
    """A command a task was waiting on was run, rejected or expired: tell the session what
    happened and queue the task again, so the listener resumes it (works from any process)."""
    from jarvis.agent import resume_note
    task_id = payload.get("task_id")
    t = get(mem, task_id) if task_id else None
    if not t or t["status"] != "waiting" or t["waiting_on"] != action_id:
        return False
    steps = steps_of(t)
    ok = not result.startswith(("(Elijah rejected", "(the approval request expired", "(couldn't"))
    if steps:
        steps[-1]["ok"] = ok
    blocked = json.loads(t["blocked"] or "{}") or {"command": " ".join(payload.get("argv", []))}
    _save(mem, task_id, status="queued", waiting_on=None, blocked=None, steps=steps,
          resume_note=resume_note(blocked, result))
    return True


def recover(mem: Memory) -> int:
    """After a restart: tasks cut off mid-run go back in the queue, to resume their session."""
    rows = mem.db.execute("SELECT * FROM tasks WHERE status = 'running'").fetchall()
    for t in rows:
        _save(mem, t["id"], status="queued", resume_note=t["resume_note"] or (
            "JARVIS restarted while you were working. Check what finished, then carry on."
            if t["session_id"] else None))
    return len(rows)


def stop(mem: Memory, task_id: int | None = None) -> str:
    """Stop a task (the newest active one by default). A command it was waiting on is cancelled;
    a running session is killed within a second or so."""
    if task_id is None:
        row = mem.db.execute(f"SELECT * FROM tasks WHERE status IN {ACTIVE} ORDER BY id DESC LIMIT 1").fetchone()
    else:
        row = get(mem, task_id)
    if not row or row["status"] not in ACTIVE:
        return "No task is running, sir." if task_id is None else "That task isn't running, sir."
    if row["waiting_on"]:
        pending = mem.get_action(row["waiting_on"])
        if pending and pending["status"] == "pending":
            mem.resolve_action(row["waiting_on"], "rejected")
    _save(mem, row["id"], status="stopped", waiting_on=None, summary="Stopped by Elijah.")
    mem.log("task", f"#{row['id']} stopped")
    return f"Stopped the task ({row['goal'][:60]}), sir."


def parse_stop(text: str) -> tuple[bool, int | None]:
    m = STOP_WORDS.match(text or "")
    return (True, int(m["id"]) if m["id"] else None) if m else (False, None)


def show(mem: Memory, task_id: int) -> str:
    """For `jarvis tasks show`: the task, its steps, and where its session log is."""
    t = get(mem, task_id)
    if not t:
        return f"No task #{task_id}"
    lines = [f"#{t['id']} [{t['status']}] {t['goal']}", f"folder: {folder_of(t)}"
             + ("" if editable(folder_of(t)) else " (read-only)"),
             f"runs: {t['runs']}, working time: {int(t['active_seconds'] // 60)} min, session: {t['session_id'] or '-'}"]
    lines += [f"  {'✓' if s.get('ok') else '✗' if s.get('ok') is False else '…'} {s['n']}. {s['update']}"
              for s in steps_of(t)]
    if t["summary"]:
        lines.append(f"\n{t['summary']}")
    log = state_dir(task_id) / "session.jsonl"
    if log.exists():
        lines.append(f"\nFull session log: {log}")
    return "\n".join(lines)
