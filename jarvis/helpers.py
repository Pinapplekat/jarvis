"""JARVIS keeping an eye on its helpers, so it can say how things are going without guessing.

Its own helpers (tasks, background jobs, the self-edit build) are tracked where they run. On top
of that, every Claude Code session on this Mac writes a live transcript under ~/.claude/projects,
and that's how JARVIS sees sessions it didn't start itself, like a "jarvis-build" session in a
screen window or one open in the desktop app: what each is working on, its last steps and words,
and whether it's busy or has gone quiet. Read-only, from files on this Mac. Transcript text is
redacted and is untrusted data to JARVIS, never instructions.
"""

import json
import time
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
         "steps": [], "age": now - path.stat().st_mtime, "scripted": False}
    for e in _tail_events(path):
        t = e.get("type")
        if t == "ai-title":
            s["title"] = e.get("aiTitle") or s["title"]
        elif t == "last-prompt":
            s["last_prompt"] = e.get("lastPrompt") or s["last_prompt"]
        elif t in ("user", "assistant"):
            s["folder"] = e.get("cwd") or s["folder"]
            s["scripted"] = e.get("entrypoint") == "sdk-cli"  # a one-shot `claude -p` run
            msg = e.get("message") or {}
            if t == "user" and (text := _user_text(msg).strip()) and not e.get("toolUseResult"):
                s["last_prompt"] = text
            if t == "assistant":
                for c in msg.get("content") or []:
                    if c.get("type") == "tool_use":
                        s["steps"].append(describe_tool(c.get("name", ""), c.get("input") or {}))
                    elif c.get("type") == "text" and c.get("text", "").strip():
                        s["said"] = c["text"].strip()
    s["steps"] = s["steps"][-5:]
    s["state"] = "working now" if s["age"] < WORKING_SECONDS else f"quiet for {_mins(s['age'])}"
    for key in ("title", "last_prompt", "said"):
        s[key] = redact(" ".join(str(s[key]).split()), limit=300)
    s["steps"] = [redact(" ".join(x.split()), limit=120) for x in s["steps"]]
    return s


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
    st = selfedit.read_status()
    if st:
        alive = selfedit.builder_alive()
        lines.append(f"- your self-edit build: {'running' if alive else 'not running'}"
                     + (f", {st.get('title', '')} ({st.get('phase', '')}), last step: {st.get('last_update', '-')}" if alive else ""))
    elif not selfedit.enabled():
        lines.append("- your self-edit build: off (self-editing isn't enabled, so approved plans wait for a Claude "
                     "Code session to build them)")
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
