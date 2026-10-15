"""Deliver alerts: 'high' is a Telegram message, 'urgent' is a message plus a phone call.

Urgent calls wait out quiet hours; the message still goes out immediately.
"""

from datetime import datetime, time
from typing import Protocol

from jarvis.memory import Memory


class Messenger(Protocol):
    def send(self, text: str) -> object: ...


class Caller(Protocol):
    def call(self, message: str) -> None: ...


def parse_quiet_hours(spec: str) -> tuple[time, time] | None:
    """'23-7' -> (23:00, 07:00). Empty string disables quiet hours."""
    if not spec.strip():
        return None
    start, end = (int(h) for h in spec.split("-"))
    return time(start), time(end)


def in_quiet_hours(now: time, window: tuple[time, time] | None) -> bool:
    if window is None:
        return False
    start, end = window
    if start <= end:
        return start <= now < end
    return now >= start or now < end


def deliver_alerts(
    mem: Memory, messenger: Messenger | None, caller: Caller | None,
    now: datetime, quiet: tuple[time, time] | None,
) -> tuple[int, int]:
    """Returns (messages sent, calls placed). `now` is local wall-clock time.
    A missing messenger or caller just skips that channel."""
    messages = calls = 0
    quiet_now = in_quiet_hours(now.time(), quiet)
    for alert in mem.undelivered_alerts():
        urgent = alert["level"] == "urgent"
        if alert["texted_at"] is None and messenger:
            note = ""
            if urgent and not caller:
                note = " (no call: Twilio isn't set up)"
            elif urgent and quiet_now:
                note = " (calling you after quiet hours)"
            messenger.send(f"{'🚨 URGENT' if urgent else '🔔'} {alert['message']}{note}")
            mem.mark_alert(alert["id"], "texted_at")
            messages += 1
        if urgent and caller and alert["called_at"] is None and not quiet_now:
            caller.call(alert["message"])
            mem.mark_alert(alert["id"], "called_at")
            mem.log("alert_called", alert["message"])
            calls += 1
    return messages, calls
