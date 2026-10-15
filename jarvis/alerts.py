"""Deliver alerts: 'high' is a text, 'urgent' is a text plus a phone call.

Urgent calls wait out quiet hours; the text still goes out immediately.
"""

from datetime import datetime, time

from jarvis.memory import Memory
from jarvis.sms import SmsClient


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
    mem: Memory, client: SmsClient, me: str, now: datetime, quiet: tuple[time, time] | None
) -> tuple[int, int]:
    """Returns (texts sent, calls placed). `now` is local wall-clock time."""
    texts = calls = 0
    quiet_now = in_quiet_hours(now.time(), quiet)
    for alert in mem.undelivered_alerts():
        if alert["texted_at"] is None:
            prefix = "URGENT" if alert["level"] == "urgent" else "JARVIS"
            note = " (calling you after quiet hours)" if alert["level"] == "urgent" and quiet_now else ""
            client.send(me, f"{prefix}: {alert['message']}{note}")
            mem.mark_alert(alert["id"], "texted_at")
            texts += 1
        if alert["level"] == "urgent" and alert["called_at"] is None and not quiet_now:
            client.call(me, alert["message"])
            mem.mark_alert(alert["id"], "called_at")
            mem.log("alert_called", alert["message"])
            calls += 1
    return texts, calls
