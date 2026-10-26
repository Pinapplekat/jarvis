import json
import threading

from jarvis import timing
from jarvis.memory import Memory


def test_a_turn_records_steps_and_logs_one_event():
    m = Memory(":memory:")
    timing.start("desk")
    with timing.step("transcribe"):
        pass
    timing.mark("model_1", 1.25)
    timing.mark("model_1", 0.25)
    timing.since_start("first_text")
    out = timing.finish(m, first_audio=2.0)
    assert out["channel"] == "desk" and out["model_1"] == 1.5 and out["first_audio"] == 2.0
    assert "transcribe" in out and out["total"] >= 0
    (event,) = [e for e in m.recent_events() if e["kind"] == "timing"]
    assert json.loads(event["content"])["model_1"] == 1.5
    assert timing.current() is None and timing.finish(m) is None


def test_ensure_only_starts_when_nothing_is_running():
    assert timing.ensure("telegram") is True
    assert timing.ensure("phone") is False and timing.current()["channel"] == "telegram"
    timing.finish()


def test_marks_without_a_turn_do_nothing_and_turns_are_per_thread():
    timing.mark("route", 1.0)  # no turn: ignored
    timing.start("desk")
    seen = []
    t = threading.Thread(target=lambda: seen.append(timing.current()))
    t.start()
    t.join()
    assert seen == [None]
    timing.finish()


def test_summary_reports_median_and_p90():
    rows = [{"channel": "desk", "total": float(i)} for i in range(1, 11)]
    text = timing.summary(rows)
    assert "total" in text and "10 turns" in text
    assert timing.summary([]) == "No per-step timings yet."


def test_context_snapshot_leaves_out_bookkeeping_events():
    m = Memory(":memory:")
    m.log("note", "Shipped 3 orders")
    for _ in range(30):
        m.log("timing", "{}")
        m.log("listen_error", "getUpdates: ConnectError")
    snap = m.context_snapshot()
    assert "Shipped 3 orders" in snap and "ConnectError" not in snap and "timing" not in snap
