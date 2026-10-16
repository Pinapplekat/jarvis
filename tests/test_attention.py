from datetime import datetime, timedelta, timezone

import pytest

from jarvis import config
from jarvis.attention import Attention, explain_recent, stuck_signals
from jarvis.judge import Answer, Question, claude_engine
from jarvis.memory import Memory
from jarvis.voice import Announcer

NOON = datetime(2026, 10, 15, 19, 0, tzinfo=timezone.utc)  # noon Pacific


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setattr(config, "ATTENTION", True)
    monkeypatch.setattr(config, "QUIET_HOURS", "")
    monkeypatch.setattr(config, "ATTENTION_THRESHOLD", 0.75)
    monkeypatch.setattr(config, "ATTENTION_DAILY_LIMIT", 2)
    monkeypatch.setattr(config, "ATTENTION_MIN_GAP_MINUTES", 15)
    monkeypatch.setattr(config, "ATTENTION_REVIEW_MINUTES", 10)


def fail(m, cmd, minutes_ago, code=1):
    m.observe("terminal", f"$ {cmd}  [exit {code}, 0.5s]", at=NOON - timedelta(minutes=minutes_ago))


def test_stuck_signals():
    m = Memory(":memory:")
    assert stuck_signals(m, NOON) == []
    for t in (8, 5, 2):
        fail(m, "pytest tests/test_cart.py", t)
    fail(m, "pytest tests/test_cart.py", 30)  # too old
    assert stuck_signals(m, NOON) == ["the same command failed 3 times in 10 min: $ pytest tests/test_cart.py"]

    m2 = Memory(":memory:")
    for i, t in enumerate((9, 7, 5, 3)):
        fail(m2, f"cmd{i}", t)
    assert stuck_signals(m2, NOON) == ["4 failing terminal commands in 10 min"]

    m3 = Memory(":memory:")
    for t in (9, 6, 3):
        m3.observe("screen", "Traceback (most recent call last):\n ImportError: no cart", app="Code",
                   at=NOON - timedelta(minutes=t))
    assert stuck_signals(m3, NOON) == ["error output on screen 3 times in 10 min (Code)"]


class FakeAnnouncer:
    def __init__(self):
        self.sent = []

    def send(self, text, spoken=None):
        self.sent.append((text, spoken))


def engine(speak=True, conf=0.9, kind="stuck_help", urgency=0.5):
    calls = []

    def decide(state, questions):
        calls.append(state)
        return {"speak": Answer(speak, conf), "kind": Answer(kind, 0.8), "urgency": Answer(urgency, 0.8)}
    decide.calls = calls
    return decide


def mind(state, kind, signals):
    return {"spoken": "Sir, that import was renamed yesterday.", "text": "The cart module moved; try `from shop.cart import Cart`."}


def make(m, decide, clock):
    return Attention(m, decide, FakeAnnouncer(), mind=mind, now=lambda: clock[0])


def test_stuck_triggers_review_and_speaks_with_split_spoken_text():
    m, clock = Memory(":memory:"), [NOON]
    for t in (8, 5, 2):
        fail(m, "pytest", t)
    a = make(m, engine(), clock)
    assert a.maybe_review(active=True) == "spoke (stuck_help, 0.90)"
    assert a.announcer.sent == [("The cart module moved; try `from shop.cart import Cart`.",
                                 "Sir, that import was renamed yesterday.")]
    assert m.chat_history(1)[0]["text"].startswith("The cart module moved")
    assert "pytest" in explain_recent(m) and "stuck_help" in explain_recent(m)
    clock[0] = NOON + timedelta(minutes=2)
    assert a.maybe_review(active=True) is None  # stuck cooldown, and periodic not due yet


def test_inactive_or_disabled_never_reviews(monkeypatch):
    m, clock = Memory(":memory:"), [NOON]
    fail(m, "x", 1)
    decide = engine()
    assert make(m, decide, clock).maybe_review(active=False) is None
    monkeypatch.setattr(config, "ATTENTION", False)
    assert make(m, decide, clock).maybe_review(active=True) is None
    assert decide.calls == []


def test_periodic_review_needs_new_activity_and_interval():
    m, clock = Memory(":memory:"), [NOON]
    decide = engine(speak=False, conf=0.9, kind="none")
    a = make(m, decide, clock)
    assert a.maybe_review(active=True) is None  # nothing observed
    m.observe("screen", "editing", app="Code", at=NOON - timedelta(minutes=1))
    assert a.maybe_review(active=True).startswith("stayed quiet")
    clock[0] = NOON + timedelta(minutes=5)
    m.observe("screen", "more", app="Code", at=clock[0])
    assert a.maybe_review(active=True) is None
    clock[0] = NOON + timedelta(minutes=11)
    assert a.maybe_review(active=True).startswith("stayed quiet")
    assert len(decide.calls) == 2


def test_threshold_gap_urgency_and_daily_limit():
    m, clock = Memory(":memory:"), [NOON]
    a = make(m, engine(conf=0.6), clock)
    assert a.review(NOON, "periodic", []) == "stayed quiet (below confidence bar 0.75; speak=True 0.60)"
    no = make(m, engine(speak=False, conf=0.9, kind="none"), clock)
    assert no.review(NOON, "periodic", []) == "stayed quiet (judge said no; speak=False 0.90)"

    a = make(m, engine(conf=0.9, urgency=0.5), clock)
    assert a.review(NOON, "periodic", []).startswith("spoke")
    assert a.review(NOON + timedelta(minutes=5), "periodic", []) .startswith("stayed quiet (spoke too recently;")
    urgent = make(m, engine(conf=0.9, urgency=0.95), clock)
    assert urgent.review(NOON + timedelta(minutes=6), "stuck", []).startswith("spoke")  # urgency beats gap
    assert a.review(NOON + timedelta(minutes=40), "periodic", []) .startswith("stayed quiet (daily limit reached;")
    assert len(m.recent_judgments(limit=10)) == 6


def test_stuck_review_uses_lower_bar(monkeypatch):
    monkeypatch.setattr(config, "ATTENTION_STUCK_THRESHOLD", 0.5)
    m = Memory(":memory:")
    a = make(m, engine(conf=0.55), [NOON])
    assert a.review(NOON, "periodic", []).startswith("stayed quiet (below confidence bar 0.75")
    assert a.review(NOON, "stuck", ["same command failed 3 times"]).startswith("spoke")


def test_quiet_hours_block(monkeypatch):
    local_hour = NOON.astimezone().hour
    monkeypatch.setattr(config, "QUIET_HOURS", f"{local_hour}-{(local_hour + 1) % 24}")
    m = Memory(":memory:")
    a = make(m, engine(), [NOON])
    assert a.review(NOON, "stuck", ["x"]) .startswith("stayed quiet (quiet hours;")
    assert a.announcer.sent == []


def make_quick(m, clock, fast_answer):
    calls = []

    def fast(state, questions):
        calls.append(state)
        return fast_answer
    a = Attention(m, engine(), FakeAnnouncer(), mind=mind, now=lambda: clock[0], fast=fast)
    return a, calls


QUIET = {"speak": Answer(False, 0.9), "kind": Answer("none", 0.9), "urgency": Answer(0.0, 0.9)}
HELP = {"speak": Answer(True, 0.85), "kind": Answer("heads_up", 0.8), "urgency": Answer(0.4, 0.8)}


def test_quick_checks_follow_new_activity_at_most_every_interval(monkeypatch):
    monkeypatch.setattr(config, "ATTENTION_QUICK", True)
    monkeypatch.setattr(config, "ATTENTION_QUICK_SECONDS", 45)
    m, clock = Memory(":memory:"), [NOON]
    a, calls = make_quick(m, clock, QUIET)
    assert a.maybe_review(active=True) is None and calls == []      # nothing observed yet
    m.observe("screen", "editing cart.py", app="Code", at=NOON)
    assert a.maybe_review(active=True) is None and len(calls) == 1  # looked, stayed quiet
    clock[0] = NOON + timedelta(seconds=20)
    m.observe("screen", "more edits", app="Code", at=clock[0])
    assert a.maybe_review(active=True) is None and len(calls) == 1  # too soon
    clock[0] = NOON + timedelta(seconds=50)
    assert a.maybe_review(active=True) is None and len(calls) == 2
    clock[0] = NOON + timedelta(seconds=100)
    assert a.maybe_review(active=True) is None and len(calls) == 2  # nothing new since last look
    assert m.recent_judgments() == []                                 # quiet glances aren't logged
    assert m.get_kv(f"attention_quick_{NOON.astimezone().date().isoformat()}") == "2"


def test_quick_yes_speaks_through_the_normal_rules(monkeypatch):
    monkeypatch.setattr(config, "ATTENTION_QUICK", True)
    m, clock = Memory(":memory:"), [NOON]
    a, calls = make_quick(m, clock, HELP)
    m.observe("screen", "deploy failed: missing env var STRIPE_KEY", app="Terminal", at=NOON)
    assert a.maybe_review(active=True) == "spoke (heads_up, 0.85)"
    assert a.announcer.sent and m.recent_judgments()[0]["trigger"] == "quick"
    clock[0] = NOON + timedelta(minutes=2)
    m.observe("screen", "still failing", app="Terminal", at=clock[0])
    assert a.maybe_review(active=True).startswith("stayed quiet (spoke too recently")


def test_quick_checks_skipped_when_speaking_is_impossible(monkeypatch):
    monkeypatch.setattr(config, "ATTENTION_QUICK", True)
    monkeypatch.setattr(config, "ATTENTION_DAILY_LIMIT", 0)
    m, clock = Memory(":memory:"), [NOON]
    a, calls = make_quick(m, clock, HELP)
    m.observe("screen", "x", app="Code", at=NOON)
    assert a.maybe_review(active=True) is None and calls == []


def test_stuck_still_reviews_immediately_with_quick_mode(monkeypatch):
    monkeypatch.setattr(config, "ATTENTION_QUICK", True)
    m, clock = Memory(":memory:"), [NOON]
    a, calls = make_quick(m, clock, QUIET)
    for t in (8, 5, 2):
        fail(m, "pytest", t)
    assert a.maybe_review(active=True).startswith("spoke (stuck_help")
    assert calls == []  # stuck path used the full review, not a glance


def test_claude_engine_parses_typed_answers():
    seen = {}

    def runner(system, prompt, schema):
        seen["schema"] = schema
        return {"speak": {"value": True, "confidence": 0.8}, "kind": {"value": "heads_up", "confidence": 0.7}}
    decide = claude_engine(runner)
    out = decide("situation", [Question("speak", "?", "yesno"),
                               Question("kind", "?", "choice", ["heads_up", "none"])])
    assert out["speak"] == Answer(True, 0.8) and out["kind"].value == "heads_up"
    assert seen["schema"]["properties"]["kind"]["properties"]["value"]["enum"] == ["heads_up", "none"]


def test_announcer_speaks_spoken_version():
    spoken = []
    a = Announcer(None, None, speaker=lambda t: spoken.append(t) or True, presence=lambda now: True)
    a.send("long text with `code`", spoken="Short spoken line.")
    assert spoken == ["Short spoken line."]
