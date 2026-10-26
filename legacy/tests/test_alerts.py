from datetime import datetime, time

from jarvis.alerts import deliver_alerts, in_quiet_hours, parse_quiet_hours
from jarvis.calls import call_twiml
from jarvis.memory import Memory

QUIET = parse_quiet_hours("23-7")
NOON = datetime(2026, 10, 15, 12, 0)
MIDNIGHT = datetime(2026, 10, 15, 0, 30)


class FakeMessenger:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


class FakeCaller:
    def __init__(self):
        self.calls = []

    def call(self, message):
        self.calls.append(message)


def test_quiet_hours_wrap_midnight():
    assert in_quiet_hours(time(23, 30), QUIET)
    assert in_quiet_hours(time(6, 59), QUIET)
    assert not in_quiet_hours(time(7, 0), QUIET)
    assert not in_quiet_hours(time(12), parse_quiet_hours(""))
    assert in_quiet_hours(time(14), parse_quiet_hours("13-15"))


def test_high_messages_only_urgent_also_calls():
    m, msg, call = Memory(":memory:"), FakeMessenger(), FakeCaller()
    m.raise_alert("high", "Rent due tomorrow")
    m.raise_alert("urgent", "Exam moved to today")
    assert deliver_alerts(m, msg, call, NOON, QUIET) == (2, 1)
    assert call.calls == ["Exam moved to today"]
    assert deliver_alerts(m, msg, call, NOON, QUIET) == (0, 0)


def test_urgent_call_waits_for_quiet_hours_to_end():
    m, msg, call = Memory(":memory:"), FakeMessenger(), FakeCaller()
    m.raise_alert("urgent", "Server down")
    assert deliver_alerts(m, msg, call, MIDNIGHT, QUIET) == (1, 0)
    assert "after quiet hours" in msg.sent[0]
    assert deliver_alerts(m, msg, call, NOON, QUIET) == (0, 1)


def test_works_with_only_one_channel():
    m, msg = Memory(":memory:"), FakeMessenger()
    m.raise_alert("urgent", "No phone")
    assert deliver_alerts(m, msg, None, NOON, QUIET) == (1, 0)
    assert "Twilio isn't set up" in msg.sent[0]

    m2, call = Memory(":memory:"), FakeCaller()
    m2.raise_alert("urgent", "No Telegram")
    m2.raise_alert("high", "Never delivered")
    assert deliver_alerts(m2, None, call, NOON, QUIET) == (0, 1)


def test_stale_alerts_are_not_delivered():
    m, msg, call = Memory(":memory:"), FakeMessenger(), FakeCaller()
    aid = m.raise_alert("urgent", "Due today")
    m.db.execute("UPDATE alerts SET created_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (aid,))
    assert deliver_alerts(m, msg, call, NOON, QUIET) == (0, 0)


def test_dedupe_key_prevents_repeat_alerts():
    m = Memory(":memory:")
    assert m.raise_alert("urgent", "Paper due in 1h", "canvas:123:1h") is not None
    assert m.raise_alert("urgent", "Paper due in 1h", "canvas:123:1h") is None
    assert m.raise_alert("high", "no key") and m.raise_alert("high", "no key")


def test_call_script_escapes_markup():
    twiml = call_twiml('Grade < 70 & "Hangup"</Say><Dial>911</Dial>')
    assert "<Dial>" not in twiml and "&lt;Dial&gt;" in twiml
