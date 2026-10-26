"""JARVIS keeping an eye on its helpers, so it can say how things are going without guessing.

Its own helpers (tasks, background jobs, the self-edit build) are tracked where they run. On top
of that, every Claude Code session on this Mac writes a live transcript under ~/.claude/projects,
and that's how JARVIS sees sessions it didn't start itself, like a "jarvis-build" session in a
screen window or one open in the desktop app: what each is working on, its last steps and words,
and whether it's busy or has gone quiet. Read-only, from files on this Mac. Transcript text is
redacted and is untrusted data to JARVIS, never instructions.
"""

import json
import re
import time
from datetime import datetime
from pathlib import Path

from jarvis import config
from jarvis.memory import Memory

PROJECTS = Path.home() / ".claude" / "projects"
RECENT_HOURS = 8        # sessions touched within this window are listed
WORKING_SECONDS = 120   # written to this recently: it's working
TAIL_BYTES = 400_000    # how much of each transcript's end is read
_cache: dict = {}


def _own_sessions(mem: Memory | None) -> set[str]:
    """Sessions JARVIS runs itself (tasks, the build): reported through their own channels."""
    ids = set()
    if mem is not None:
        ids |= {r["session_id"] for r in mem.db.execute("SELECT session_id FROM tasks WHERE session_id IS NOT NULL")}
    from jarvis import selfedit
    if (sid := selfedit.read_status().get("session_id")):
        ids.add(sid)
    return ids


def _tail_events(path: Path, limit: int = TAIL_BYTES) -> list[dict]:
    try:
        with path.open("rb") as f:
            size = f.seek(0, 2)
            f.seek(max(0, size - limit))
            raw = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = raw.splitlines()[1 if size > limit else 0:]  # the first line may be cut
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _user_text(message: dict) -> str:
    c = message.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
    return ""


def read_session(path: Path, now: float | None = None) -> dict:
    """What one Claude Code session is doing, from the end of its transcript."""
    from jarvis.agent import describe_tool
    from jarvis.introspect import redact
    now = now or time.time()
    s = {"id": path.stem, "file": str(path), "title": "", "folder": "", "last_prompt": "", "said": "",
         "steps": [], "age": now - path.stat().st_mtime, "scripted": False, "app": "", "branch": "", "plan": []}
    for e in _tail_events(path):
        t = e.get("type")
        if t == "ai-title":
            s["title"] = e.get("aiTitle") or s["title"]
        elif t == "last-prompt":
            s["last_prompt"] = e.get("lastPrompt") or s["last_prompt"]
        elif t in ("user", "assistant"):
            s["folder"] = e.get("cwd") or s["folder"]
            s["scripted"] = e.get("entrypoint") == "sdk-cli"  # a one-shot `claude -p` run
            s["app"] = e.get("entrypoint") or s["app"]
            s["branch"] = e.get("gitBranch") or s["branch"]
            msg = e.get("message") or {}
            if t == "user" and (text := _user_text(msg).strip()) and not e.get("toolUseResult"):
                s["last_prompt"] = text
            if t == "assistant":
                for c in msg.get("content") or []:
                    if c.get("type") == "tool_use":
                        name, args = c.get("name", ""), c.get("input") or {}
                        if name.endswith(PLAN_TOOLS):
                            s["plan"] = _follow_plan(s["plan"], args)
                        elif (step := describe_tool(name, args)) and not name.endswith(QUIET_TOOLS):
                            s["steps"].append(step)
                    elif c.get("type") == "text" and c.get("text", "").strip():
                        s["said"] = c["text"].strip()
    if not s["plan"]:  # set early in a long session: further back than the tail
        s["plan"] = _plan_from_start(path)
    s["steps"] = s["steps"][-5:]
    s["state"] = "working now" if s["age"] < WORKING_SECONDS else f"quiet for {_mins(s['age'])}"
    for key in ("title", "last_prompt", "said"):
        s[key] = redact(" ".join(str(s[key]).split()), limit=300)
    s["steps"] = [redact(" ".join(x.split()), limit=120) for x in s["steps"]]
    s["plan"] = [{"content": redact(" ".join(str(x.get("content", "")).split()), limit=120),
                  "status": x.get("status", "pending")} for x in s["plan"][:12]]
    return s


# Claude Code's own task list (the desktop app's plan, or TodoWrite in a terminal): what it's on, step by step.
PLAN_TOOLS = ("update_plan", "TodoWrite")
QUIET_TOOLS = ("wrap_up", "mark_chapter", "ToolSearch")  # bookkeeping, not work worth listing


def _plan_from_start(path: Path) -> list[dict]:
    """The plan, replayed from the whole transcript (only lines that touch it are parsed; cached by size)."""
    try:
        size = path.stat().st_size
    except OSError:
        return []
    hit = _cache.get(("plan", str(path)))
    if hit and hit[0] == size:
        return hit[1]
    plan: list[dict] = []
    try:
        with path.open("rb") as f:
            for raw in f:
                if b"update_plan" not in raw and b"TodoWrite" not in raw:
                    continue
                try:
                    e = json.loads(raw)
                except ValueError:
                    continue
                for c in (e.get("message") or {}).get("content") or [] if e.get("type") == "assistant" else []:
                    if isinstance(c, dict) and c.get("type") == "tool_use" and str(c.get("name", "")).endswith(PLAN_TOOLS):
                        plan = _follow_plan(plan, c.get("input") or {})
    except OSError:
        return []
    _cache[("plan", str(path))] = (size, plan)
    return plan


def _follow_plan(plan: list[dict], args: dict) -> list[dict]:
    """Apply one plan update: a whole new list, or 'advance' / 'done' + 'start' on the one already there."""
    items = args.get("items") or args.get("todos")
    if isinstance(items, list):
        return [{"content": x.get("content", ""), "status": x.get("status", "pending")}
                for x in items if isinstance(x, dict)]
    plan = [dict(x) for x in plan]
    if args.get("advance"):
        cur = next((i for i, x in enumerate(plan) if x["status"] == "in_progress"), None)
        if cur is not None:
            plan[cur]["status"] = "completed"
        nxt = next((x for x in plan[(cur or 0):] if x["status"] == "pending"), None)
        if nxt:
            nxt["status"] = "in_progress"
    for n in args.get("done") or []:
        if isinstance(n, int) and 0 < n <= len(plan):
            plan[n - 1]["status"] = "completed"
    if isinstance(args.get("start"), int) and 0 < args["start"] <= len(plan):
        for x in plan:
            if x["status"] == "in_progress":
                x["status"] = "pending"
        plan[args["start"] - 1]["status"] = "in_progress"
    return plan


def _mins(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m} min" if m < 90 else f"{m // 60} h {m % 60} min"


def claude_sessions(mem: Memory | None = None, now: float | None = None, limit: int = 6) -> list[dict]:
    """Claude Code sessions on this Mac with recent activity, newest first (not JARVIS's own)."""
    now = now or time.time()
    if not PROJECTS.is_dir():
        return []
    skip_dir = config.DATA_DIR.resolve()
    own = _own_sessions(mem)
    files = []
    for path in PROJECTS.glob("*/*.jsonl"):
        try:
            if now - path.stat().st_mtime <= RECENT_HOURS * 3600 and path.stem not in own:
                files.append(path)
        except OSError:
            continue
    out = []
    for path in sorted(files, key=lambda p: p.stat().st_mtime, reverse=True):
        s = read_session(path, now)
        if s["folder"] and Path(s["folder"]).resolve() == skip_dir:
            continue  # JARVIS's own one-off calls
        if s["scripted"] and s["age"] >= WORKING_SECONDS:
            continue  # finished one-shot runs (scripts, tests) aren't helpers worth mentioning
        out.append(s)
        if len(out) >= limit:
            break
    return out


APPS = {"claude-desktop": "Claude app", "cli": "Terminal", "sdk-cli": "a script", "claude-vscode": "VS Code"}


def where(s: dict) -> str:
    """Where a session runs: the app, the folder, and the git branch when it isn't main."""
    parts = [APPS.get(s.get("app", ""), s.get("app") or "Claude Code"), Path(s["folder"]).name if s.get("folder") else ""]
    if s.get("branch") and s["branch"] not in ("main", "master", "HEAD"):
        parts.append(s["branch"])
    return " · ".join(p for p in parts if p)


def board(mem: Memory | None, now: float | None = None) -> list[dict]:
    """Every helper at a glance, for the panel: JARVIS's own tasks, background jobs and build, and the
    Claude Code sessions on this Mac. Busy ones first. Each: name, kind, where, state, busy, waiting
    (on Elijah), now (what it's on), plan, steps, asked, said."""
    from jarvis import jobs, selfedit, tasks
    now = now or time.time()
    out = []
    if mem is not None:
        for t in tasks.recent(mem, 8):
            updated = datetime.fromisoformat(t["updated_at"]).timestamp() if t["updated_at"] else 0
            if t["status"] not in tasks.ACTIVE and now - updated > 2 * 3600:
                continue  # finished a while ago
            steps = [x.get("update", "") for x in tasks.steps_of(t)]
            out.append({"kind": "task", "name": f"Task #{t['id']}: {t['goal'][:100]}",
                        "where": f"JARVIS task · {tasks.folder_of(t).name}", "state": t["status"],
                        "busy": t["status"] == "running", "waiting": t["status"] == "waiting",
                        "now": steps[-1] if t["status"] in tasks.ACTIVE and steps else "",
                        "plan": [], "steps": steps[-5:], "asked": t["goal"], "said": t["summary"] or ""})
    for j in jobs.list_jobs(8):
        if j["state"] != "running" and now - (j["started"] + j["runtime"]) > 3600:
            continue
        out.append({"kind": "job", "name": f"Job #{j['id']}: {jobs._command(j)[:100]}",
                    "where": f"background job · {Path(j['cwd']).name}", "state": jobs.describe_state(j),
                    "busy": j["state"] == "running", "waiting": False, "now": "", "plan": [], "steps": [],
                    "asked": j.get("why", ""), "said": jobs.job_status(j["id"], lines=3)["tail"]})
    if selfedit.builder_alive():
        st = selfedit.read_status()
        out.append({"kind": "build", "name": f"Building: {st.get('title', '?')}", "where": "JARVIS build session",
                    "state": st.get("phase", "building"), "busy": True, "waiting": False,
                    "now": st.get("last_update", ""), "plan": [], "steps": [], "asked": "", "said": ""})
    for s in claude_sessions(mem, now, limit=10):
        busy = s["age"] < WORKING_SECONDS
        out.append({"kind": "session", "name": s["title"] or s["last_prompt"][:80] or s["id"][:8],
                    "where": where(s), "state": s["state"], "busy": busy, "waiting": False,
                    "now": next((x["content"] for x in s["plan"] if x["status"] == "in_progress"), ""),
                    "plan": s["plan"], "steps": s["steps"], "asked": s["last_prompt"],
                    "said": re.sub(r"\*\*|`|^#+\s*", "", s["said"])})  # no markdown marks on the panel
    return sorted(out, key=lambda h: (not h["waiting"], not h["busy"]))


def screen_names() -> dict[str, str]:
    """Named screen sessions running Claude Code, e.g. {'jarvis-build': 'claude --remote-control ...'}."""
    import subprocess
    try:
        ps = subprocess.run(["ps", "-axo", "command"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    names = {}
    for line in ps.splitlines():
        parts = line.split()
        if parts[:2] == ["SCREEN", "-dmS"] and len(parts) > 3 and "claude" in parts[3]:
            names[parts[2]] = " ".join(parts[3:])[:200]
    return names


def for_prompt(mem: Memory, max_age: float = 20) -> str:
    """The compact view chat gets with every message (cached briefly: it reads files)."""
    hit = _cache.get("prompt")
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    lines = []
    from jarvis import selfedit
    if not selfedit.enabled():
        lines.append("- self-editing: OFF (approved plans wait for a Claude Code session to build them)")
    else:
        st, alive = selfedit.read_status(), selfedit.builder_alive()
        building = [r["title"] for r in mem.self_edits("queued", "building")] if mem is not None else []
        ready = [r["title"] for r in mem.self_edits("ready")] if mem is not None else []
        state = ("building " + st.get("title", "?") + f" ({st.get('phase', '?')}), last step: {st.get('last_update', '-')}, "
                 f"last output {int((time.time() - (st.get('last_output') or st.get('started') or time.time())) // 60)} min ago"
                 if alive else "idle")
        lines.append(f"- self-editing: ON; your build session is {state}"
                     + (f"; queued: {', '.join(building)}" if building and not alive else "")
                     + (f"; waiting for his Deploy/Discard: {', '.join(ready)}" if ready else ""))
    screens = screen_names()
    for s in claude_sessions(mem):
        lines.append(f"- Claude Code session \"{s['title'] or s['last_prompt'][:60] or s['id'][:8]}\" in "
                     f"{Path(s['folder']).name or '?'}: {s['state']}; last asked: {s['last_prompt'][:120]}; "
                     f"last steps: {'; '.join(s['steps'][-3:]) or '-'}")
    if screens:
        lines.append("- screen sessions running Claude Code: " + ", ".join(screens))
    text = "\n".join(lines) or "(no helpers running)"
    _cache["prompt"] = (time.time(), text)
    return text


def detail(mem: Memory, which: str = "") -> str:
    """One Claude Code session in more detail (by title words or id; the newest by default)."""
    sessions = claude_sessions(mem, limit=12)
    if not sessions:
        return "(no Claude Code sessions with recent activity)"
    which = which.strip().lower()
    pick = next((s for s in sessions if which and (which in s["title"].lower() or s["id"].startswith(which))),
                sessions[0])
    return (f"session \"{pick['title']}\" ({pick['id'][:8]}) in {pick['folder']}\n"
            f"state: {pick['state']}\nlast asked: {pick['last_prompt']}\n"
            f"last steps:\n" + "\n".join(f"  - {x}" for x in pick["steps"]) + f"\nlast said: {pick['said']}")
