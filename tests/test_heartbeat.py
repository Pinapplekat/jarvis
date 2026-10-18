from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from jarvis import config, heartbeat
from jarvis.canvas import Deadline
from jarvis.memory import Memory

TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 17, 14, 0, tzinfo=TZ)


@pytest.fixture(autouse=True)
def limits(monkeypatch):
    monkeypatch.setattr(config, "HEARTBEAT", True)
    monkeypatch.setattr(config, "HEARTBEAT_MIN_INTERVAL_MIN", 30)
    monkeypatch.setattr(config, "HEARTBEAT_MAX_RUNS_PER_DAY", 5)
    monkeypatch.setattr(config, "HEARTBEAT_MAX_MSGS_PER_DAY", 2)
    monkeypatch.setattr(config, "HEARTBEAT_MIN_MSG_GAP_MIN", 90)


def thinker(*outs):
    calls = []

    def think(system, prompt):
        calls.append(prompt)
        out = outs[min(len(calls), len(outs)) - 1]
        if isinstance(out, Exception):
            raise out
        return out
    think.calls = calls
    return think


SAY = {"notify": True, "message": "ECON Module 3 is due in five hours, sir, and you haven't opened it.", "urgency": "high"}
QUIET = {"notify": False, "message": "", "urgency": "low", "why": "nothing new"}


def essay(hours=5, title="Module 3 essay"):
    return Deadline("canvas:1", "ECON", title, NOW + timedelta(hours=hours), None, "unsubmitted", True)


def later(minutes):
    return NOW + timedelta(minutes=minutes)


def test_kill_switch_pause_and_quiet_hours_skip_the_model(monkeypatch):
    m = Memory(":memory:")
    never = thinker(AssertionError("called"))
    assert heartbeat.run_heartbeat(m, NOW, quiet=True, think=never) == "quiet hours"
    heartbeat.set_paused(m, True)
    assert heartbeat.run_heartbeat(m, NOW, think=never) == "paused"
    heartbeat.set_paused(m, False)
    monkeypatch.setattr(config, "HEARTBEAT", False)
    assert heartbeat.run_heartbeat(m, NOW, think=never).startswith("off")
    assert never.calls == []


def test_quiet_by_default_and_every_decision_logged():
    m = Memory(":memory:")
    assert heartbeat.run_heartbeat(m, NOW, [essay()], think=thinker(QUIET)) == "quiet"
    assert m.undelivered_alerts() == []
    assert "quiet: nothing new" in m.recent_events(1)[0]["content"]


def test_a_message_becomes_a_high_alert_with_the_snapshot_as_data():
    m = Memory(":memory:")
    think = thinker(SAY)
    assert heartbeat.run_heartbeat(m, NOW, [essay()], think=think) == "sent"
    [alert] = m.undelivered_alerts()
    assert alert["level"] == "high" and "Module 3" in alert["message"]
    assert "ECON: Module 3 essay" in think.calls[0] and "untrusted" in think.calls[0]
    assert m.pending_actions() == []  # it never drafts email or queues anything


def test_unchanged_snapshot_costs_nothing():
    m = Memory(":memory:")
    think = thinker(QUIET)
    heartbeat.run_heartbeat(m, NOW, [essay()], think=think)
    assert heartbeat.run_heartbeat(m, later(31), [essay()], think=think) == "nothing changed"
    assert len(think.calls) == 1
    assert heartbeat.run_heartbeat(m, later(62), [essay(), essay(20, "Quiz 4")], think=think) == "quiet"
    assert len(think.calls) == 2


def test_min_interval_between_runs():
    m = Memory(":memory:")
    think = thinker(QUIET)
    heartbeat.run_heartbeat(m, NOW, [essay()], think=think)
    assert heartbeat.run_heartbeat(m, later(10), [essay(20, "new")], think=think) == "not due"


def test_daily_run_cap(monkeypatch):
    monkeypatch.setattr(config, "HEARTBEAT_MAX_RUNS_PER_DAY", 2)
    m = Memory(":memory:")
    think = thinker(QUIET)
    for i in range(2):
        heartbeat.run_heartbeat(m, later(31 * i), [essay(20 + i, f"t{i}")], think=think)
    assert heartbeat.run_heartbeat(m, later(100), [essay(30, "new")], think=think) == "daily run cap reached"
    assert len(think.calls) == 2
    # a new day resets the counters
    assert heartbeat.run_heartbeat(m, NOW + timedelta(days=1), [essay(40, "x")], think=think) == "quiet"


def test_daily_message_cap_and_min_gap():
    m = Memory(":memory:")
    think = thinker(SAY)
    assert heartbeat.run_heartbeat(m, NOW, [essay(5, "a")], think=think) == "sent"
    assert heartbeat.run_heartbeat(m, later(31), [essay(5, "b")], think=think) == "held back (min gap)"
    assert heartbeat.run_heartbeat(m, later(95), [essay(5, "c")], think=think) == "sent"
    assert heartbeat.run_heartbeat(m, later(200), [essay(5, "d")], think=think) == "held back (daily message cap)"
    assert len(m.undelivered_alerts()) == 2


def test_low_urgency_stays_quiet():
    m = Memory(":memory:")
    assert heartbeat.run_heartbeat(m, NOW, [essay()], think=thinker({**SAY, "urgency": "low"})) == "held back (low urgency)"
    assert m.undelivered_alerts() == []


@pytest.mark.parametrize("out", [{"notify": "yes", "message": "x", "urgency": "high"}, {}, RuntimeError("CLI failed")])
def test_malformed_or_failed_output_stays_quiet(out):
    m = Memory(":memory:")
    assert heartbeat.run_heartbeat(m, NOW, [essay()], think=thinker(out)).startswith("quiet")
    assert m.undelivered_alerts() == []


def test_force_runs_even_when_nothing_changed():
    m = Memory(":memory:")
    think = thinker(QUIET)
    heartbeat.run_heartbeat(m, NOW, [essay()], think=think)
    heartbeat.run_heartbeat(m, later(1), [essay()], think=think, force=True)
    assert len(think.calls) == 2


def test_its_own_logs_dont_count_as_change():
    m = Memory(":memory:")
    first = heartbeat.snapshot(m, NOW, [essay()])
    heartbeat.set_paused(m, True)
    heartbeat.set_paused(m, False)
    m.log("answered", "1.2s total")
    assert heartbeat.snapshot(m, NOW, [essay()]) == first


def test_status_line():
    m = Memory(":memory:")
    heartbeat.run_heartbeat(m, NOW, [essay()], think=thinker(QUIET))
    assert "on; today 1/5 runs, 0/2 messages" in heartbeat.status(m, NOW)
