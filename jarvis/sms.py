"""Text email drafts to the user via Twilio and turn their replies into approvals.

Replies are polled from Twilio's API rather than received by webhook, so this runs
anywhere (the Mac today, a cloud box later) without a public URL.
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Protocol
from xml.sax.saxutils import escape

import httpx

from jarvis import config
from jarvis.memory import Memory

API = "https://api.twilio.com/2010-04-01"
MAX_BODY_PREVIEW = 400

REPLY = re.compile(
    r"^\s*(?P<verb>yes|y|ok|approve|send|no|n|reject|cancel)(?![a-z])[\s#]*(?P<id>\d+)?\s*[.!]*\s*$",
    re.IGNORECASE,
)
APPROVE_WORDS = {"yes", "y", "ok", "approve", "send"}


@dataclass
class InboundSms:
    sid: str
    sender: str
    body: str
    sent_at: datetime


class SmsClient(Protocol):
    def send(self, to: str, body: str) -> None: ...
    def call(self, to: str, message: str) -> None: ...
    def inbound(self, since: datetime) -> list[InboundSms]: ...


class TwilioClient:
    def __init__(self):
        missing = [k for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER",
                               "MY_PHONE_NUMBER") if not getattr(config, k)]
        if missing:
            raise SystemExit(f"Set {', '.join(missing)} in .env")
        self.http = httpx.Client(
            base_url=f"{API}/Accounts/{config.TWILIO_ACCOUNT_SID}",
            auth=(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN),
            timeout=30,
        )

    def send(self, to: str, body: str) -> None:
        r = self.http.post("/Messages.json", data={
            "From": config.TWILIO_FROM_NUMBER, "To": to, "Body": body,
        })
        if not r.is_success:
            raise RuntimeError(f"Twilio send failed ({r.status_code}): {r.text[:300]}")

    def call(self, to: str, message: str) -> None:
        r = self.http.post("/Calls.json", data={
            "From": config.TWILIO_FROM_NUMBER, "To": to, "Twiml": call_twiml(message),
        })
        if not r.is_success:
            raise RuntimeError(f"Twilio call failed ({r.status_code}): {r.text[:300]}")

    def inbound(self, since: datetime) -> list[InboundSms]:
        r = self.http.get("/Messages.json", params={
            "To": config.TWILIO_FROM_NUMBER,
            "DateSent>": since.strftime("%Y-%m-%d"),
            "PageSize": 50,
        })
        r.raise_for_status()
        return [
            InboundSms(m["sid"], m["from"], m["body"], parsedate_to_datetime(m["date_sent"]))
            for m in r.json()["messages"]
            if m["direction"] == "inbound"
        ]


def call_twiml(message: str) -> str:
    said = escape(f"This is JARVIS with an urgent alert. {message}")
    return (
        f'<Response><Pause length="1"/><Say voice="Polly.Matthew">{said}</Say>'
        f'<Pause length="1"/><Say voice="Polly.Matthew">Again. {escape(message)}</Say></Response>'
    )


def parse_reply(text: str) -> tuple[bool, int | None] | None:
    """'YES 3' -> (True, 3); 'no' -> (False, None); anything else -> None."""
    m = REPLY.match(text)
    if not m:
        return None
    approve = m["verb"].lower() in APPROVE_WORDS
    return approve, int(m["id"]) if m["id"] else None


def format_draft(action_id: int, payload: dict) -> str:
    body = payload["body"]
    if len(body) > MAX_BODY_PREVIEW:
        body = body[:MAX_BODY_PREVIEW].rstrip() + "…"
    return (
        f"JARVIS email #{action_id}\n"
        f"To: {', '.join(payload['to'])}\n"
        f"Subject: {payload['subject']}\n\n"
        f"{body}\n\n"
        f"Reply YES {action_id} to send, NO {action_id} to cancel."
    )


def notify_pending(mem: Memory, client: SmsClient, to: str) -> int:
    count = 0
    for action in mem.unnotified_actions():
        if action["kind"] != "send_email":
            continue
        client.send(to, format_draft(action["id"], json.loads(action["payload"])))
        mem.mark_notified(action["id"])
        count += 1
    return count


def _notified_at(action) -> datetime | None:
    return datetime.fromisoformat(action["notified_at"]) if action["notified_at"] else None


def handle_replies(mem: Memory, client: SmsClient, me: str) -> int:
    """Apply YES/NO replies from `me`. Returns how many actions changed state."""
    changed = 0
    since = datetime.now(timezone.utc) - timedelta(days=2)
    for sms in sorted(client.inbound(since), key=lambda s: s.sent_at):
        if sms.sender != me or not mem.claim_sms(sms.sid):
            continue

        parsed = parse_reply(sms.body)
        if parsed is None:
            client.send(me, "Didn't catch that. Reply YES <number> or NO <number>.")
            continue
        approve, action_id = parsed

        # Only drafts announced before this text was sent can be answered by it.
        open_actions = [
            a for a in mem.pending_actions(kind="send_email")
            if (n := _notified_at(a)) and n <= sms.sent_at
        ]
        if action_id is None:
            if len(open_actions) != 1:
                ids = ", ".join(f"#{a['id']}" for a in open_actions) or "none"
                client.send(me, f"Which one? Waiting for approval: {ids}. Reply YES <number>.")
                continue
            action_id = open_actions[0]["id"]
        elif action_id not in {a["id"] for a in open_actions}:
            action = mem.get_action(action_id)
            state = action["status"] if action else "not found"
            client.send(me, f"#{action_id} can't be changed ({state}).")
            continue

        mem.resolve_action(action_id, "approved" if approve else "rejected")
        mem.log("sms_reply", f"#{action_id} {'approved' if approve else 'rejected'} by text")
        client.send(me, f"Approved #{action_id}. Sending when your Mac is awake."
                    if approve else f"Cancelled #{action_id}.")
        changed += 1
    return changed
