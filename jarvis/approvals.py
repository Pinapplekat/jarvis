"""Confirmations for risky actions: email drafts, texts and calls, and high-risk commands.

Each one goes to Telegram as a plain-words question ("is that what you meant?") with buttons
as a shortcut. A clear typed or spoken yes ("yes", "go ahead", a voice note saying "yes, send
it") does the same as the button, a clear no cancels, and a reply that starts like an answer but
isn't clear ("yes but...") cancels too and says so. A bare yes resolves to the single open item;
with several open, JARVIS asks which (by what they are, not by number). Only the configured chat
is obeyed, a reply only counts for items that had been sent before it was written, and command
requests expire. Text from pages, emails or files never reaches here: only Elijah's messages do.
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from jarvis.memory import Memory
from jarvis.telegram import TelegramClient, Update

MAX_BODY_PREVIEW = 1500
OFFSET_KEY = "telegram_offset"
SIRI_KINDS = ("send_text", "make_call")   # texts and calls: high risk, his yes on the exact recipient and text
DECIDED_HERE = ("send_email", *SIRI_KINDS)  # approved by _decide (commands go through their own buttons)

# 'yes 3' / 'no #4': the id form, still accepted (ids are never shown, but buttons carry them).
REPLY = re.compile(
    r"^\s*(?P<verb>yes|y|ok|approve|send|no|n|reject|cancel)(?![a-z])[\s#]*(?P<id>\d+)?\s*[.!]*\s*$",
    re.IGNORECASE,
)
APPROVE_WORDS = {"yes", "y", "ok", "approve", "send"}
_FILL = r"(?:[\s,]+(?:please|sir|thanks|thank you|do it|go ahead|run it|send it|go for it|that's it|cancel it|don'?t))*"
CLEAR_YES = re.compile(
    r"^\s*(?:yes|yep|yeah|yup|y|ok|okay|sure|confirm(?:ed)?|approved?|go ahead|do it|run it|send it|go for it|"
    r"correct|that'?s right|that is right|that'?s what i meant|affirmative)" + _FILL + r"\s*[.!]*\s*$", re.I)
CLEAR_NO = re.compile(
    r"^\s*(?:no|nope|nah|n|cancel|reject|don'?t|do not|never ?mind|negative|hold off)" + _FILL + r"\s*[.!]*\s*$", re.I)
ANSWERISH = re.compile(r"^\s*(?:yes|yeah|yep|yup|ok|okay|sure|no|nope|nah|don'?t)\b", re.I)


def parse_reply(text: str) -> tuple[bool, int | None] | None:
    """'YES 3' -> (True, 3); 'no' -> (False, None); anything else -> None."""
    m = REPLY.match(text)
    if not m:
        return None
    return m["verb"].lower() in APPROVE_WORDS, int(m["id"]) if m["id"] else None


def parse_confirmation(text: str) -> str | None:
    """'yes' | 'no' for a clear answer, 'unclear' for something that starts like one, else None."""
    if (old := parse_reply(text)) is not None and old[1] is None:
        return "yes" if old[0] else "no"
    if CLEAR_YES.match(text or ""):
        return "yes"
    if CLEAR_NO.match(text or ""):
        return "no"
    if ANSWERISH.match(text or ""):
        return "unclear"
    return None


# ----- what each item is, in words -----
def describe(action) -> str:
    p = json.loads(action["payload"])
    if action["kind"] == "send_email":
        return f"the email to {', '.join(p['to'])} (\"{p['subject'][:60]}\")"
    if action["kind"] in SIRI_KINDS:
        from jarvis.siri import describe as describe_siri
        return describe_siri(action)
    from jarvis.terminal import describe_action
    return f"running {describe_action(p)}"


def format_draft(action_id: int, payload: dict) -> str:
    body = payload["body"]
    if len(body) > MAX_BODY_PREVIEW:
        body = body[:MAX_BODY_PREVIEW].rstrip() + "…"
    return (
        f"Email from {payload['from']}\n"
        f"To: {', '.join(payload['to'])}\n"
        f"Subject: {payload['subject']}\n\n"
        f"{body}\n\n"
        f"Is that what you meant, sir? Reply yes to send it to {', '.join(payload['to'])}, no to cancel."
    )


def notify_pending(mem: Memory, tg: TelegramClient, say: Callable[[str], None] | None = None) -> int:
    """Send the question for new email drafts and for commands waiting for an OK. `say` asks
    out loud as well (when he's at the Mac)."""
    from jarvis.terminal import notify_commands
    count = notify_commands(mem, tg, say=say)
    for action in mem.unnotified_actions():
        if action["kind"] in SIRI_KINDS:
            from jarvis.siri import format_request
            aid = action["id"]
            tg.send(format_request(action), buttons=[("Approve ✅", f"approve:{aid}"), ("Reject ✖️", f"reject:{aid}")])
            if say:
                say(f"Sir, {describe(action)} is waiting for your OK. Is that what you meant? It's in Telegram.")
            mem.mark_notified(aid)
            count += 1
            continue
        if action["kind"] != "send_email":
            continue
        aid = action["id"]
        p = json.loads(action["payload"])
        tg.send(format_draft(aid, p), buttons=[("Approve ✅", f"approve:{aid}"), ("Reject ✖️", f"reject:{aid}")])
        if say:
            say(f"Sir, the email to {', '.join(p['to'])} is ready. Is that what you meant? It's in Telegram.")
        mem.mark_notified(aid)
        count += 1
    return count


# ----- deciding -----
def _open(mem: Memory, written_at: datetime, kinds=(*DECIDED_HERE, "run_command")) -> list:
    from jarvis.terminal import _expired
    now = datetime.now(timezone.utc)
    return [a for kind in kinds for a in mem.pending_actions(kind=kind)
            if a["notified_at"] and datetime.fromisoformat(a["notified_at"]) <= written_at
            and not (kind != "send_email" and _expired(a, now))]  # commands, texts and calls lapse


def _decide(mem: Memory, approve: bool, action_id: int | None, written_at: datetime) -> tuple[bool, str]:
    """Apply a decision on an email draft, text or call. Returns (changed, message for the user).
    An approved text or call goes out at once, bound to exactly what he was shown."""
    from jarvis import siri
    from jarvis.mailrelay import approve as approve_email
    siri.expire(mem)
    open_drafts = _open(mem, written_at, kinds=DECIDED_HERE)
    if action_id is None:
        if len(open_drafts) != 1:
            which = "; ".join(describe(a) for a in open_drafts) or "none"
            return False, f"Which one, sir? Waiting for your OK: {which}. Tap its button."
        action_id = open_drafts[0]["id"]
    elif action_id not in {a["id"] for a in open_drafts}:
        action = mem.get_action(action_id)
        return False, f"That one can't be changed now ({action['status'] if action else 'not found'})."
    action = mem.get_action(action_id)
    if approve and action["kind"] in SIRI_KINDS:
        siri.approve(mem, action_id)
    elif approve:
        approve_email(mem, action_id)
    else:
        mem.resolve_action(action_id, "rejected")
    mem.log("approval", f"#{action_id} {'approved' if approve else 'rejected'} via Telegram")
    if approve and action["kind"] in SIRI_KINDS:
        return True, f"Approved {describe(action)}. {siri.execute(mem, action_id)}"
    if approve:
        return True, f"Approved {describe(action)}. Sending when your Mac is awake."
    return True, f"Cancelled {describe(action)}."


@dataclass
class Confirmed:
    note: str                 # what to tell him
    to_chat: bool = False     # also pass the message on to chat (an unclear answer was cancelled)


def confirm_by_words(mem: Memory, text: str, written_at: datetime,
                     on_command: Callable[[str, int], str]) -> Confirmed | None:
    """A typed or spoken answer to the open question(s), or None if it isn't one (then it's
    ordinary chat). `on_command(verb, id)` runs or rejects a command the same way its buttons do."""
    answer = parse_confirmation(text)
    items = _open(mem, written_at) if answer else []
    if not items:
        return None
    # If JARVIS has said something in chat since the question went out ("Shall I remind you at
    # nine?"), a bare "yes" most likely answers that. The item keeps its buttons.
    said = mem.db.execute("SELECT at FROM chat_log WHERE role = 'assistant' ORDER BY id DESC LIMIT 1").fetchone()
    newest = max(datetime.fromisoformat(a["notified_at"]) for a in items)
    if said and datetime.fromisoformat(said["at"]) > newest:
        return None
    if answer == "unclear":
        if len(items) != 1:
            return None  # can't tell which; let chat sort it out
        item = items[0]
        if item["kind"] in DECIDED_HERE:
            mem.resolve_action(item["id"], "rejected")
        else:
            on_command("cmd_reject", item["id"])
        mem.log("approval", f"#{item['id']} cancelled: unclear answer")
        return Confirmed(f"I've held off on {describe(item)}, sir; that wasn't a clear yes. "
                         "Ask again if you'd like it, and answer plain yes.", to_chat=True)
    if len(items) > 1:
        return Confirmed("Which one, sir? Waiting for your OK: " + "; ".join(describe(a) for a in items)
                         + ". Tap the button on the one you mean.")
    item = items[0]
    if item["kind"] in DECIDED_HERE:
        return Confirmed(_decide(mem, answer == "yes", item["id"], written_at)[1])
    toast = on_command("cmd_run" if answer == "yes" else "cmd_reject", item["id"])
    if toast in ("Already handled",) or toast.startswith("Expired"):
        return Confirmed(f"Too late for {describe(item)}, sir: {toast.lower()}.")
    return Confirmed(f"Running {describe(item).removeprefix('running ')}, sir." if answer == "yes"
                     else f"Cancelled {describe(item)}.")


def handle_updates(mem: Memory, tg: TelegramClient, allowed_user: int,
                   on_text: Callable[[Update], None] | None = None, wait: int = 0,
                   on_button: Callable[[str, int], str] | None = None,
                   transcribe: Callable[[bytes], str] | None = None,
                   on_location: Callable[[Update], None] | None = None) -> int:
    """Apply button presses and typed (or spoken) answers; pass other messages to on_text and
    other buttons ("verb:id") to on_button, which returns the toast text. Voice notes are
    transcribed first, so a spoken "yes, send it" works too. Returns how many drafts changed state."""
    offset = mem.get_kv(OFFSET_KEY)
    changed = 0
    for u in tg.updates(int(offset) if offset else None, wait):
        mem.set_kv(OFFSET_KEY, str(u.update_id + 1))  # never process an update twice
        if u.sender_id != allowed_user:
            continue  # strangers' voice notes are never even downloaded
        if u.location:
            if on_location:
                on_location(u)
            continue
        if u.voice_file_id:
            if not transcribe:
                continue
            tg.action("typing")
            u.text = transcribe(tg.download(u.voice_file_id))
            u.via_voice = True
            if not u.text:
                tg.send("Didn't catch that, sir. Mind trying again?")
                continue
        changed += _handle(mem, tg, u, on_text, on_button)
    return changed


def _handle(mem: Memory, tg: TelegramClient, u: Update, on_text=None, on_button=None) -> bool:
    if u.callback_id:
        verb, _, raw_id = (u.callback_data or "").partition(":")
        if not raw_id.isdigit():
            tg.answer(u.callback_id, "Unknown button")
            return False
        if verb not in ("approve", "reject"):
            toast = on_button(verb, int(raw_id)) if on_button else "Unknown button"
            tg.answer(u.callback_id, toast)
            if u.message_id and toast not in ("Unknown button", "Already handled"):
                tg.edit(u.message_id, f"{u.message_text or ''}\n\n— {toast}")
            return False
        # A button can only exist on a draft that was already sent, so any time works.
        did, msg = _decide(mem, verb == "approve", int(raw_id), datetime.max.replace(tzinfo=u.sent_at.tzinfo))
        tg.answer(u.callback_id, msg[:200])
        if did and u.message_id:
            tg.edit(u.message_id, f"{u.message_text or ''}\n\n— {msg}")
        return did
    if u.text:
        parsed = parse_reply(u.text)
        if parsed is not None and parsed[1] is not None:  # 'yes 3': an explicit id
            action = mem.get_action(parsed[1])
            if action and action["kind"] == "run_command" and on_button:
                tg.send(on_button("cmd_run" if parsed[0] else "cmd_reject", parsed[1]))
                return False
            did, msg = _decide(mem, parsed[0], parsed[1], u.sent_at)
            tg.send(msg)
            return did
        before = len(mem.pending_actions(status="approved", kind="send_email"))
        done = confirm_by_words(mem, u.text, u.sent_at, on_button or (lambda verb, aid: "Unknown button"))
        if done is None:  # not an answer to anything open: ordinary chat ("ok" to a remark, too)
            if on_text:
                on_text(u)
            return False
        tg.send(done.note)
        if done.to_chat and on_text:
            on_text(u)
        return len(mem.pending_actions(status="approved", kind="send_email")) > before
    return False
