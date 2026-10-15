from datetime import datetime, time

from jarvis.alerts import deliver_alerts, in_quiet_hours, parse_quiet_hours
from jarvis.memory import Memory
from jarvis.sms import call_twiml

ME = "+12065550100"
QUIET = parse_quiet_hours("23-7")
NOON = datetime(2026, 10, 15, 12, 0)
MIDNIGHT = datetime(2026, 10, 15, 0, 30)


class FakeTwilio:
    def __init__(self):
        self.texts, self.calls = [], []

    def send(self, to, body):
        self.texts.append(body)

    def call(self, to, message):
        self.calls.append(message)


def test_quiet_hours_wrap_midnight():
    assert in_quiet_hours(time(23, 30), QUIET)
    assert in_quiet_hours(time(6, 59), QUIET)
    assert not in_quiet_hours(time(7, 0), QUIET)
    assert not in_quiet_hours(time(12), parse_quiet_hours(""))
    assert in_quiet_hours(time(14), parse_quiet_hours("13-15"))


def test_high_texts_only_urgent_also_calls():
    m, t = Memory(":memory:"), FakeTwilio()
    m.raise_alert("high", "Rent due tomorrow")
    m.raise_alert("urgent", "Exam moved to today")
    assert deliver_alerts(m, t, ME, NOON, QUIET) == (2, 1)
    assert t.calls == ["Exam moved to today"]
    assert deliver_alerts(m, t, ME, NOON, QUIET) == (0, 0)


def test_urgent_call_waits_for_quiet_hours_to_end():
    m, t = Memory(":memory:"), FakeTwilio()
    m.raise_alert("urgent", "Server down")
    assert deliver_alerts(m, t, ME, MIDNIGHT, QUIET) == (1, 0)
    assert "after quiet hours" in t.texts[0]
    assert deliver_alerts(m, t, ME, NOON, QUIET) == (0, 1)


def test_dedupe_key_prevents_repeat_alerts():
    m = Memory(":memory:")
    assert m.raise_alert("urgent", "Paper due in 1h", "canvas:123:1h") is not None
    assert m.raise_alert("urgent", "Paper due in 1h", "canvas:123:1h") is None
    assert m.raise_alert("high", "no key") and m.raise_alert("high", "no key")


def test_call_script_escapes_markup():
    twiml = call_twiml('Grade < 70 & "Hangup"</Say><Dial>911</Dial>')
    assert "<Dial>" not in twiml and "&lt;Dial&gt;" in twiml
