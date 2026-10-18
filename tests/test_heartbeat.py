from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from jarvis import config, heartbeat
from jarvis.canvas import Deadline
from jarvis.judge import Answer
from jarvis.memory import Memory

TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 17, 14, 0, tzinfo=TZ)


@pytest.fixture(autouse=True)
def limits(monkeypatch):
    monkeypatch.setattr(config, "HEARTBEAT", True)
    monkeypatch.setattr(config, "HEARTBEAT_MIN_INTERVAL_MIN", 30)
    monkeypatch.setattr(config, "HEARTBEAT_JEV_INTERVAL_MIN", 10)
    monkeypatch.setattr(config, "HEARTBEAT_MAX_RUNS_PER_DAY", 5)
    monkeypatch.setattr(config, "HEARTBEAT_RUNAWAY_PER_DAY", 10)


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


def jev(worth, confidence=0.9, urgency="normal", fail=False):
    calls = []

    def decide(state, questions):
        calls.append(state)
        if fail:
            raise RuntimeError("Jev down")
        return {"worth_saying": Answer(worth, confidence), "urgency": Answer(urgency, 0.8)}
    decide.calls = calls
    return decide


SAY = {"notify": True, "message": "ECON Module 3 is due in five hours, sir, and you haven't opened it.", "urgency": "high"}
QUIET = {"notify": False, "message": "", "urgency": "low", "why": "nothing new"}


def essay(hours=5, title="Module 3 essay"):
    return Deadline("canvas:1", "ECON", title, NOW + timedelta(hours=hours), None, "unsubmitted", True)


def later(minutes):
    return NOW + timedelta(minutes=minutes)


def test_kill_switch_and_pause_skip_everything(monkeypatch):
    m = Memory(":memory:")
    never = thinker(AssertionError("called"))
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


def test_a_message_becomes_an_alert_and_joins_the_conversation():
    m = Memory(":memory:")
    think = thinker(SAY)
    assert heartbeat.run_heartbeat(m, NOW, [essay()], think=think) == "sent"
    [alert] = m.undelivered_alerts()
    assert alert["level"] == "high" and "Module 3" in alert["message"]
    assert m.chat_history(1)[0]["text"] == SAY["message"]  # chat and the next look know it was said
    assert "ECON: Module 3 essay" in think.calls[0] and "untrusted" in think.calls[0]
    assert m.pending_actions() == []  # it never drafts email or queues anything


def test_no_quota_important_things_keep_getting_through():
    m = Memory(":memory:")
    think = thinker(SAY)
    for i, hours in enumerate([20, 10, 5, 2]):  # four messages in a few hours: its call, not a cap
        assert heartbeat.run_heartbeat(m, later(31 * i), [essay(hours - i * 0.5)], think=think) == "sent"
    assert len(m.undelivered_alerts()) == 4


def test_it_sees_the_conversation_both_sides():
    m = Memory(":memory:")
    m.add_chat("assistant", "Module 3 is due tonight, sir.")  # stamped with the real clock
    m.add_chat("user", "I know, I'll get it done")
    now = datetime.now(TZ) + timedelta(seconds=5)
    think = thinker(QUIET)
    heartbeat.run_heartbeat(m, now, [], think=think)
    assert 'you: "Module 3 is due tonight, sir."' in think.calls[0] and "Elijah: \"I know, I'll get it done\"" in think.calls[0]


def test_his_preferences_shape_the_look():
    m = Memory(":memory:")
    m.remember("preference", "heartbeat", "back off during the week; only deadlines")
    think = thinker(QUIET)
    heartbeat.run_heartbeat(m, NOW, [essay()], think=think)
    assert "back off during the week; only deadlines" in think.calls[0]


def test_quiet_hours_only_urgent_wakes_him_the_rest_waits_for_morning():
    m = Memory(":memory:")
    assert heartbeat.run_heartbeat(m, NOW, [essay()], quiet=True, think=thinker(SAY)) == "waiting for morning"
    assert m.undelivered_alerts() == []
    # not marked as seen, so the morning look reconsiders it even though nothing changed
    assert heartbeat.run_heartbeat(m, later(121), [essay()], think=thinker(SAY)) == "sent"
    m2 = Memory(":memory:")
    urgent = {**SAY, "urgency": "urgent"}
    assert heartbeat.run_heartbeat(m2, NOW, [essay(1)], quiet=True, think=thinker(urgent)) == "sent"
    assert m2.undelivered_alerts()[0]["level"] == "urgent"


def test_unchanged_snapshot_costs_nothing_but_time_running_out_is_a_change():
    m = Memory(":memory:")
    think = thinker(QUIET)
    heartbeat.run_heartbeat(m, NOW, [essay(30)], think=think)
    assert heartbeat.run_heartbeat(m, later(31), [essay(30)], think=think) == "nothing changed"
    assert len(think.calls) == 1
    # the same deadline, now inside the 24-hour bucket: worth a fresh look
    assert heartbeat.run_heartbeat(m, NOW + timedelta(hours=7), [essay(30)], think=think) == "quiet"
    assert len(think.calls) == 2


def test_min_interval_between_looks():
    m = Memory(":memory:")
    think = thinker(QUIET)
    heartbeat.run_heartbeat(m, NOW, [essay()], think=think)
    assert heartbeat.run_heartbeat(m, later(10), [essay(20, "new")], think=think) == "not due"


def test_daily_claude_cap(monkeypatch):
    monkeypatch.setattr(config, "HEARTBEAT_MAX_RUNS_PER_DAY", 2)
    m = Memory(":memory:")
    think = thinker(QUIET)
    for i in range(2):
        heartbeat.run_heartbeat(m, later(31 * i), [essay(20 + i, f"t{i}")], think=think)
    assert heartbeat.run_heartbeat(m, later(100), [essay(30, "new")], think=think) == "daily Claude cap reached"
    assert len(think.calls) == 2
    assert heartbeat.run_heartbeat(m, NOW + timedelta(days=1), [essay(40, "x")], think=think) == "quiet"


def test_runaway_guard_only_stops_a_broken_loop(monkeypatch):
    monkeypatch.setattr(config, "HEARTBEAT_RUNAWAY_PER_DAY", 2)
    m = Memory(":memory:")
    think = thinker(SAY)
    for i in range(2):
        assert heartbeat.run_heartbeat(m, later(31 * i), [essay(5, f"t{i}")], think=think) == "sent"
    assert heartbeat.run_heartbeat(m, later(100), [essay(5, "t9")], think=think) == "held back (runaway guard)"


@pytest.mark.parametrize("out", [{"notify": "yes", "message": "x", "urgency": "high"}, {}, RuntimeError("CLI failed")])
def test_malformed_or_failed_output_stays_quiet(out):
    m = Memory(":memory:")
    assert heartbeat.run_heartbeat(m, NOW, [essay()], think=thinker(out)).startswith("quiet")
    assert m.undelivered_alerts() == []


# ----- Jev's cheap first look -----
def test_a_sure_no_from_jev_costs_no_claude_call():
    m = Memory(":memory:")
    fast, think = jev(False, 0.9), thinker(AssertionError("Claude called"))
    assert heartbeat.run_heartbeat(m, NOW, [essay()], think=think, fast=fast) == "quiet (Jev)"
    assert len(fast.calls) == 1 and "ECON: Module 3 essay" in fast.calls[0]
    assert "Jev looks" in heartbeat.status(m, NOW)


def test_jev_looks_more_often_than_claude_would():
    m = Memory(":memory:")
    fast = jev(False, 0.9)
    heartbeat.run_heartbeat(m, NOW, [essay()], think=thinker(QUIET), fast=fast)
    assert heartbeat.run_heartbeat(m, later(11), [essay(20, "new")], think=thinker(QUIET), fast=fast) == "quiet (Jev)"


def test_jev_yes_or_unsure_hands_it_to_claude_who_makes_the_call():
    for fast in (jev(True, 0.9, "high"), jev(False, 0.5)):
        m = Memory(":memory:")
        think = thinker(SAY)
        assert heartbeat.run_heartbeat(m, NOW, [essay()], think=think, fast=fast) == "sent"
        assert "fast first look" in think.calls[0]


def test_jev_down_means_claude_looks():
    m = Memory(":memory:")
    think = thinker(QUIET)
    assert heartbeat.run_heartbeat(m, NOW, [essay()], think=think, fast=jev(False, fail=True)) == "quiet"
    assert len(think.calls) == 1


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


# ----- not nagging, in code -----
def say_about(item, urgency="normal", text="Sir, the Econ work is still unstarted."):
    return {"notify": True, "message": text, "urgency": urgency, "about": [item]}


def test_an_item_it_already_raised_isnt_repeated_unless_more_urgent():
    m = Memory(":memory:")
    d = essay(20)
    assert heartbeat.run_heartbeat(m, NOW, [d], think=thinker(say_about("deadline:canvas:1"))) == "sent"
    later_look = heartbeat.run_heartbeat(m, later(40), [essay(20, "Module 3 essay"), essay(30, "other")],
                                         think=thinker(say_about("deadline:canvas:1")))
    assert later_look == "held back (already said, not more urgent)"
    assert heartbeat.run_heartbeat(m, later(80), [d, essay(31, "x")],
                                   think=thinker(say_about("deadline:canvas:1", "high"))) == "sent"  # escalated
    assert len(m.undelivered_alerts()) == 2


def test_i_know_closes_the_item_for_good(monkeypatch):
    import time
    m = Memory(":memory:")
    now = datetime.now(TZ)
    assert heartbeat.run_heartbeat(m, now, [essay(20)], think=thinker(say_about("deadline:canvas:1"))) == "sent"
    time.sleep(1.1)
    m.add_chat("user", "I know my econ work is due, don't worry about me")
    later_now = datetime.now(TZ) + timedelta(minutes=40)
    think = thinker(say_about("deadline:canvas:1", "high"))
    out = heartbeat.run_heartbeat(m, later_now, [essay(20)], think=think)
    assert out in ("quiet", "held back (he said he's handled it)")
    assert "Module 3 essay" not in (think.calls[0] if think.calls else "")  # it's gone from what it sees


def test_jev_decides_whether_a_reply_closes_it():
    import time
    m = Memory(":memory:")
    now = datetime.now(TZ)
    heartbeat.run_heartbeat(m, now, [essay(20)], think=thinker(say_about("deadline:canvas:1")))
    time.sleep(1.1)
    m.add_chat("user", "what's the weather like")
    fast = lambda state, qs: {q.key: Answer(False, 0.9) for q in qs}  # noqa: E731  (not a dismissal)
    heartbeat.notice_dismissals(m, datetime.now(TZ) + timedelta(minutes=5), fast)
    assert heartbeat.open_items(m, datetime.now(TZ), [essay(20)])  # still open


def test_chat_dont_remind_closes_matching_items():
    m = Memory(":memory:")
    heartbeat.dont_remind(m, "Module 3 essay")
    assert heartbeat.open_items(m, NOW, [essay(20)]) == []


def test_only_recent_unread_mail_counts():
    import json as _json
    m = Memory(":memory:")
    naive = NOW.replace(tzinfo=None)
    m.set_kv("inbox_snapshot", _json.dumps({"at": NOW.isoformat(), "messages": [
        {"id": "new1", "sender": "Prof <p@x.edu>", "subject": "Quiz moved", "received": (naive - timedelta(hours=2)).isoformat(), "read": False},
        {"id": "old1", "sender": "Twilio", "subject": "RESPONSE REQUIRED", "received": (naive - timedelta(days=3)).isoformat(), "read": False},
        {"id": "read1", "sender": "Prof", "subject": "Read already", "received": naive.isoformat(), "read": True}]}))
    ids = [iid for _, iid, _ in heartbeat.open_items(m, NOW)]
    assert ids == ["mail:new1"]


def test_its_own_failures_can_be_mentioned_by_the_way():
    m = Memory(":memory:")
    m.log("jev_fallback", "Jev 402: insufficient credits")
    snap = heartbeat.snapshot(m, datetime.now(TZ))
    assert "[health:jev_fallback] Jev (fast decisions): failed 1x, latest: Jev 402: insufficient credits" in snap
    assert "By the way, sir" in heartbeat.HEARTBEAT_SYSTEM


def test_item_ids_never_reach_him():
    m = Memory(":memory:")
    think = thinker(say_about("deadline:canvas:1", text="Sir, [deadline:canvas:1] the essay is due soon."))
    heartbeat.run_heartbeat(m, NOW, [essay(5)], think=think)
    assert m.undelivered_alerts()[0]["message"] == "Sir, the essay is due soon."
