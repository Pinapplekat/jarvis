from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from jarvis.chat import apply_actions, build_prompt
from jarvis.memory import Memory
from jarvis.reminders import deliver_reminders

TZ = ZoneInfo("America/Los_Angeles")


class FakeMessenger:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


def _in(minutes: int) -> str:
    return (datetime.now(TZ) + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M")


def test_remind_action_schedules_and_shows_in_context():
    m = Memory(":memory:")
    assert apply_actions(m, [{"type": "remind", "at": _in(60), "text": "econ time"}], TZ) == []
    (r,) = m.upcoming_reminders()
    assert r["text"] == "econ time"
    assert f"#{r['id']}" in build_prompt(m, "hi", datetime.now(TZ), "")


def test_bad_reminders_are_rejected():
    m = Memory(":memory:")
    problems = apply_actions(m, [
        {"type": "remind", "at": _in(-30), "text": "past"},
        {"type": "remind", "at": datetime.now(TZ).strftime("%Y-%m-%d"), "text": "no time"},
        {"type": "remind", "at": "2099-01-01 09:00", "text": "too far"},
        {"type": "cancel_reminder", "id": 42},
    ], TZ)
    assert len(problems) == 4 and m.upcoming_reminders() == []


def test_due_reminders_sent_once_and_logged_to_chat():
    m, msg = Memory(":memory:"), FakeMessenger()
    now = datetime(2026, 10, 15, 21, 0, tzinfo=TZ)
    m.add_reminder(now - timedelta(seconds=20), "econ time")
    m.add_reminder(now + timedelta(hours=1), "later")
    assert deliver_reminders(m, msg, now) == 1
    assert msg.sent == ["⏰ econ time"]
    assert deliver_reminders(m, msg, now) == 0
    assert m.chat_history(1)[0]["text"] == "⏰ econ time"
    assert [r["text"] for r in m.upcoming_reminders()] == ["later"]


def test_late_reminders_say_so_and_cancelled_ones_never_send():
    m, msg = Memory(":memory:"), FakeMessenger()
    now = datetime(2026, 10, 16, 7, 0, tzinfo=TZ)
    m.add_reminder(datetime(2026, 10, 15, 21, 0, tzinfo=TZ), "econ time")
    rid = m.add_reminder(now - timedelta(minutes=1), "cancelled")
    assert m.cancel_reminder(rid)
    assert deliver_reminders(m, msg, now) == 1
    assert "late: this was for Thu 9:00 PM" in msg.sent[0]
