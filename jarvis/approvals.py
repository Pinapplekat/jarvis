"""Send email drafts to Telegram with Approve/Reject buttons, and apply the answers.

Typed replies like 'yes 3' or 'no' work too. Only the configured chat is obeyed, and a
reply only counts for drafts that had already been sent when the reply was written.
"""

import json
import re
from datetime import datetime
from typing import Callable

from jarvis.memory import Memory
from jarvis.telegram import TelegramClient, Update

MAX_BODY_PREVIEW = 1500
OFFSET_KEY = "telegram_offset"

REPLY = re.compile(
    r"^\s*(?P<verb>yes|y|ok|approve|send|no|n|reject|cancel)(?![a-z])[\s#]*(?P<id>\d+)?\s*[.!]*\s*$",
    re.IGNORECASE,
)
APPROVE_WORDS = {"yes", "y", "ok", "approve", "send"}


def parse_reply(text: str) -> tuple[bool, int | None] | None:
    """'YES 3' -> (True, 3); 'no' -> (False, None); anything else -> None."""
    m = REPLY.match(text)
    if not m:
        return None
    return m["verb"].lower() in APPROVE_WORDS, int(m["id"]) if m["id"] else None


def format_draft(action_id: int, payload: dict) -> str:
    body = payload["body"]
    if len(body) > MAX_BODY_PREVIEW:
        body = body[:MAX_BODY_PREVIEW].rstrip() + "…"
    return (
        f"Email #{action_id} — from {payload['from']}\n"
        f"To: {', '.join(payload['to'])}\n"
        f"Subject: {payload['subject']}\n\n"
        f"{body}"
    )


def notify_pending(mem: Memory, tg: TelegramClient) -> int:
    count = 0
    for action in mem.unnotified_actions():
        if action["kind"] != "send_email":
            continue
        aid = action["id"]
        tg.send(format_draft(aid, json.loads(action["payload"])),
                buttons=[("Approve ✅", f"approve:{aid}"), ("Reject ✖️", f"reject:{aid}")])
        mem.mark_notified(aid)
        count += 1
    return count


def _open_drafts(mem: Memory, written_at: datetime) -> list:
    return [
        a for a in mem.pending_actions(kind="send_email")
        if a["notified_at"] and datetime.fromisoformat(a["notified_at"]) <= written_at
    ]


def _decide(mem: Memory, approve: bool, action_id: int | None, written_at: datetime) -> tuple[bool, str]:
    """Apply a decision. Returns (changed, message for the user)."""
    open_drafts = _open_drafts(mem, written_at)
    if action_id is None:
        if len(open_drafts) != 1:
            ids = ", ".join(f"#{a['id']}" for a in open_drafts) or "none"
            return False, f"Which one? Waiting for approval: {ids}. Reply e.g. 'yes 3'."
        action_id = open_drafts[0]["id"]
    elif action_id not in {a["id"] for a in open_drafts}:
        action = mem.get_action(action_id)
        return False, f"#{action_id} can't be changed ({action['status'] if action else 'not found'})."
    mem.resolve_action(action_id, "approved" if approve else "rejected")
    mem.log("approval", f"#{action_id} {'approved' if approve else 'rejected'} via Telegram")
    if approve:
        return True, f"Approved #{action_id}. Sending when your Mac is awake."
    return True, f"Cancelled #{action_id}."


def handle_updates(mem: Memory, tg: TelegramClient, allowed_user: int,
                   on_text: Callable[[Update], None] | None = None, wait: int = 0) -> int:
    """Apply button presses and typed replies; pass other messages to on_text.
    Returns how many drafts changed state."""
    offset = mem.get_kv(OFFSET_KEY)
    changed = 0
    for u in tg.updates(int(offset) if offset else None, wait):
        mem.set_kv(OFFSET_KEY, str(u.update_id + 1))  # never process an update twice
        if u.sender_id != allowed_user:
            continue
        changed += _handle(mem, tg, u, on_text)
    return changed


def _handle(mem: Memory, tg: TelegramClient, u: Update, on_text=None) -> bool:
    if u.callback_id:
        verb, _, raw_id = (u.callback_data or "").partition(":")
        if verb not in ("approve", "reject") or not raw_id.isdigit():
            tg.answer(u.callback_id, "Unknown button")
            return False
        # A button can only exist on a draft that was already sent, so any time works.
        did, msg = _decide(mem, verb == "approve", int(raw_id), datetime.max.replace(tzinfo=u.sent_at.tzinfo))
        tg.answer(u.callback_id, msg)
        if did and u.message_id:
            tg.edit(u.message_id, f"{u.message_text or ''}\n\n— {msg}")
        return did
    if u.text:
        parsed = parse_reply(u.text)
        if parsed is None:
            if on_text:
                on_text(u)
            return False
        did, msg = _decide(mem, parsed[0], parsed[1], u.sent_at)
        tg.send(msg)
        return did
    return False
