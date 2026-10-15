"""Send and read mail through Apple Mail on this Mac, using whatever accounts it's signed in to."""

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from jarvis.memory import Memory

FIELD = "\x1f"
RECORD = "\x1e"

SEND_SCRIPT = """
on run argv
    set senderAddr to item 1 of argv
    set subj to item 2 of argv
    set bodyText to item 3 of argv
    tell application "Mail"
        set m to make new outgoing message with properties {subject:subj, content:bodyText, visible:false}
        tell m
            repeat with i from 4 to count of argv
                make new to recipient at end of to recipients with properties {address:(item i of argv)}
            end repeat
            set sender to senderAddr
        end tell
        send m
    end tell
end run
"""

# Mail doesn't guarantee newest-first, so read_inbox sorts by date.
READ_SCRIPT = """
on run argv
    set acctAddr to item 1 of argv
    set limitN to (item 2 of argv) as integer
    set FS to character id 31
    set RS to character id 30
    tell application "Mail"
        set acct to missing value
        repeat with a in accounts
            if (email addresses of a) contains acctAddr then set acct to a
        end repeat
        if acct is missing value then error "No Mail account with address " & acctAddr
        set box to missing value
        repeat with mb in mailboxes of acct
            if name of mb is "INBOX" or name of mb is "Inbox" then set box to contents of mb
        end repeat
        if box is missing value then error "No inbox found for " & acctAddr
        set n to count of messages of box
        if n > limitN then set n to limitN
        set out to ""
        repeat with i from 1 to n
            set msg to message i of box
            set d to date received of msg
            set stamp to ((year of d) as text) & "-" & ((month of d as integer) as text) & "-" & ¬
                ((day of d) as text) & "-" & ((time of d) as text)
            set out to out & (id of msg) & FS & (sender of msg) & FS & (subject of msg) & FS & ¬
                stamp & FS & (read status of msg) & RS
        end repeat
        return out
    end tell
end run
"""


class MailError(RuntimeError):
    pass


@dataclass
class InboxMessage:
    id: str
    sender: str
    subject: str
    received: datetime
    read: bool


def _parse_stamp(stamp: str) -> datetime:
    """Mail gives local date parts as 'Y-M-D-secondsSinceMidnight' to stay locale-independent."""
    y, mo, d, secs = (int(x) for x in stamp.split("-"))
    return datetime(y, mo, d) + timedelta(seconds=secs)


def _osascript(script: str, *args: str) -> str:
    r = subprocess.run(
        ["osascript", "-", *args], input=script, capture_output=True, text=True, timeout=60
    )
    if r.returncode != 0:
        raise MailError(r.stderr.strip())
    return r.stdout.rstrip("\n")


def send_email(sender: str, to: list[str], subject: str, body: str) -> None:
    if not to:
        raise MailError("No recipients")
    _osascript(SEND_SCRIPT, sender, subject, body, *to)


def read_inbox(account: str, limit: int = 20) -> list[InboxMessage]:
    raw = _osascript(READ_SCRIPT, account, str(limit))
    messages = []
    for rec in filter(None, raw.split(RECORD)):
        mid, sender, subject, stamp, read = rec.split(FIELD)
        messages.append(InboxMessage(mid, sender, subject, _parse_stamp(stamp), read == "true"))
    return sorted(messages, key=lambda m: m.received, reverse=True)


def list_accounts() -> list[str]:
    raw = _osascript('tell application "Mail" to get email addresses of every account')
    return [a.strip() for a in raw.split(",") if a.strip()]


def process_approved(mem: Memory, send: Callable[..., None] = send_email) -> tuple[int, int]:
    """Send every approved email in the queue. Returns (sent, failed)."""
    sent = failed = 0
    for action in mem.pending_actions(status="approved", kind="send_email"):
        p = json.loads(action["payload"])
        try:
            send(p["from"], p["to"], p["subject"], p["body"])
        except Exception as e:
            mem.resolve_action(action["id"], "failed")
            mem.log("email_failed", f"#{action['id']} to {', '.join(p['to'])}: {e}")
            failed += 1
        else:
            mem.resolve_action(action["id"], "done")
            mem.log("email_sent", f"#{action['id']} to {', '.join(p['to'])}: {p['subject']}")
            sent += 1
    return sent, failed
