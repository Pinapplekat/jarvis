"""Send reminders when they come due."""

from datetime import datetime, timedelta

from jarvis.alerts import Messenger
from jarvis.memory import Memory

LATE_AFTER = timedelta(minutes=10)


def deliver_reminders(mem: Memory, messenger: Messenger, now: datetime) -> int:
    sent = 0
    for r in mem.claim_due_reminders(now):
        due = datetime.fromisoformat(r["due_at"])
        text = f"⏰ {r['text']}"
        if now - due > LATE_AFTER:  # Mac was asleep or offline at the time
            text += f"\n(late: this was for {due.astimezone(now.tzinfo):%a %-I:%M %p})"
        messenger.send(text)
        mem.add_chat("assistant", text)  # so a follow-up like "done" has context
        sent += 1
    return sent
