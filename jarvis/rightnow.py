"""A live "right now" picture JARVIS reads before every reply: what's really running, what's
playing, whether Elijah is at the desk or mid-conversation, and what he's already been told or
has brushed off. It's built from real state each time (tasks table, events, chat log, the Mac's
idle time), so a reply can't say "the build is running" when nothing is, and doesn't repeat what
he's already heard.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Callable

from jarvis import config
from jarvis.memory import Memory

MID_CONVERSATION = timedelta(minutes=3)
TOLD_WINDOW = timedelta(hours=12)
MEDIA_WINDOW = timedelta(hours=3)


def _ago(then: datetime, now: datetime) -> str:
    mins = int((now - then).total_seconds() // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins} min ago"
    return f"{mins // 60} h {mins % 60} min ago"


def _at(row_at: str) -> datetime:
    return datetime.fromisoformat(row_at).astimezone(timezone.utc)


def running(mem: Memory, now: datetime) -> list[str]:
    try:
        rows = mem.db.execute("SELECT goal, status, updated_at FROM tasks WHERE status IN "
                              "('queued','running','waiting') ORDER BY id DESC LIMIT 5").fetchall()
    except sqlite3.Error:
        return []
    return [f"- task ({r['status']}, since {_ago(_at(r['updated_at']), now)}): {r['goal'][:120]}" for r in rows]


def media(mem: Memory, now: datetime) -> list[str]:
    rows = mem.db.execute("SELECT at, kind, content FROM events WHERE kind IN ('siri', 'media_control') "
                          "AND at >= ? ORDER BY id DESC LIMIT 3", ((now - MEDIA_WINDOW).isoformat(),)).fetchall()
    return [f"- {_ago(_at(r['at']), now)}: {r['content'][:120]}" for r in rows]


def already_told(mem: Memory, now: datetime) -> list[str]:
    """Unprompted things he's already heard (heartbeat news, reminders, alerts), newest first."""
    since = (now - TOLD_WINDOW).isoformat()
    rows = mem.db.execute("SELECT at, content FROM events WHERE kind = 'heartbeat' AND content LIKE 'sent%' "
                          "AND at >= ? ORDER BY id DESC LIMIT 6", (since,)).fetchall()
    out = []
    for r in rows:
        text = r["content"].split(": ", 1)[-1]
        out.append(f"- {_ago(_at(r['at']), now)}: {text[:140]}")
    for r in mem.db.execute("SELECT at, text FROM chat_log WHERE role = 'assistant' AND text LIKE '⏰%' AND at >= ? "
                            "ORDER BY id DESC LIMIT 4", (since,)).fetchall():
        out.append(f"- {_ago(_at(r['at']), now)} (reminder): {r['text'][:140]}")
    return out


def brushed_off(mem: Memory, now: datetime) -> list[str]:
    from jarvis.heartbeat import DISMISSIVE
    rows = mem.db.execute("SELECT at, text FROM chat_log WHERE role = 'user' AND at >= ? ORDER BY id DESC LIMIT 40",
                          ((now - TOLD_WINDOW).isoformat(),)).fetchall()
    return [f"- {_ago(_at(r['at']), now)} he said: \"{r['text'][:100]}\"" for r in rows if DISMISSIVE.search(r["text"])][:4]


def presence(mem: Memory, now: datetime, idle: Callable[[], float | None] | None) -> str:
    parts = []
    if idle is not None:
        try:
            quiet = idle()
        except Exception:
            quiet = None
        if quiet is not None:
            parts.append("at the Mac (active " + (f"{int(quiet)}s ago)" if quiet < 120 else f"{int(quiet // 60)} min ago)")
                         if quiet <= config.VOICE_IDLE_SECONDS else f"away from the Mac (idle {int(quiet // 60)} min)")
    last = mem.db.execute("SELECT at FROM chat_log WHERE role = 'user' ORDER BY id DESC LIMIT 2").fetchall()
    if len(last) == 2 and now - _at(last[1]["at"]) <= MID_CONVERSATION:
        parts.append("mid-conversation with you")
    return "; ".join(parts) or "(unknown)"


def for_prompt(mem: Memory, now: datetime, idle: Callable[[], float | None] | None = None) -> str:
    """The <right_now> block. Every part is real state; nothing is guessed."""
    now = now.astimezone(timezone.utc)
    sections = []
    try:
        sections.append("Elijah: " + presence(mem, now, idle))
        work = running(mem, now)
        sections.append("Running right now:\n" + ("\n".join(work) if work else
                        "- nothing (no task, helper or build is running; don't say one is)"))
        if (m := media(mem, now)):
            sections.append("Music lately (newest first):\n" + "\n".join(m))
        if (told := already_told(mem, now)):
            sections.append("Already told him on your own (don't bring these up again unless he asks or "
                            "something changed):\n" + "\n".join(told))
        if (off := brushed_off(mem, now)):
            sections.append("Things he brushed off (stay off those topics unless urgent):\n" + "\n".join(off))
        model = mem.get_kv("chat_model")
        if model:
            sections.append(f"Chat model he chose: {json.loads(model).get('model', '?')}")
    except (sqlite3.Error, ValueError) as e:
        sections.append(f"(partly unavailable: {str(e)[:80]})")
    return "\n".join(sections)
