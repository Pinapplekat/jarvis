"""What the brain sees besides the persona: the system prompt and the per-turn context block."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from jarvis.core import actions, conversation, sessions
from jarvis.core.apps import Registry
from jarvis.core.timeutil import ago

log = logging.getLogger("jarvis.core.context")

APP_BLOCK_MAX = 1500
RECENT_MESSAGES = 12
RECENT_ACTIONS = 5
MESSAGE_MAX = 600


def system_prompt(assistant_name: str) -> str:
    return (Path(__file__).with_name("persona.md").read_text()).replace("{name}", assistant_name).strip()


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def app_blocks(reg: Registry) -> list[str]:
    """Each enabled app's context block, truncated. A failing handler is logged and skipped."""
    blocks = []
    for app in reg.apps.values():
        if app.manifest.context is None:
            continue
        try:
            text = getattr(app.module, app.manifest.context)(app.ctx)
            if not isinstance(text, str):
                raise TypeError(f"returned {type(text).__name__}, not text")
        except Exception:
            log.exception("context handler of %s failed", app.manifest.name)
            continue
        if text.strip():
            blocks.append(_clip(text.strip(), APP_BLOCK_MAX))
    return blocks


def build_prompt(reg: Registry, text: str, channel: str, message_id: int, now: datetime | None = None,
                 images: int = 0) -> str:
    now = (now or datetime.now(timezone.utc)).astimezone()
    lines = ["[Context from Falak, not Elijah's words]",
             f"It is {now.strftime('%A %Y-%m-%d %H:%M %Z')}. Channel: {channel}."]
    history = conversation.recent(reg.conn, RECENT_MESSAGES, before_id=message_id)
    if history:
        lines.append("Recent conversation:")
        lines += [f"  {'Elijah' if m['role'] == 'user' else 'You'}: {_clip(m['text'], MESSAGE_MAX)}" for m in history]
    asks = actions.pending_asks(reg, now)
    if asks:
        lines.append("Pending asks (waiting on Elijah):")
        lines += [f"  #{a['id']} {a['question']} ({ago(a['created_at'], now)})" for a in asks]
    log_rows = actions.recent_log(reg, RECENT_ACTIONS)
    if log_rows:
        lines.append("Last actions:")
        lines += [f"  #{r['id']} {r['tool']} {r['status']} {ago(r['time'], now)}" for r in log_rows]
    lines += sessions.context_lines(reg, now)
    lines += app_blocks(reg)
    if images:
        lines.append(f"Elijah attached {images} image{'s' if images > 1 else ''}; "
                     "look at one with falak__view_attachment(n), counting from 1.")
    lines += ["[End of context]", "", "Elijah says:", text]
    return "\n".join(lines)
