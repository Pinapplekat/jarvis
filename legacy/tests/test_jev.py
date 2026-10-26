import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from jarvis import config, judge, router
from jarvis.chat import SKIPPED, build_prompt
from jarvis.judge import Answer, JevError, Question, jev_engine, with_fallback
from jarvis.memory import Memory

QS = [Question("speak", "Should JARVIS speak?"),
      Question("kind", "What kind?", "choice", ["stuck_help", "heads_up", "none"]),
      Question("urgency", "How urgent?", "score")]


def fake_jev(responses):
    """A mock Jev server: returns queued (status, body) pairs and records requests."""
    seen = []

    def handler(request):
        seen.append(request)
        status, body = responses.pop(0)
        return httpx.Response(status, json=body)
    return httpx.Client(transport=httpx.MockTransport(handler)), seen


OK = {"model": "jev-1.13.0", "usage": {"input_tokens": 300, "output_tokens": 20}, "answers": {
    "speak": {"type": "noul", "noul": 0.82},
    "kind": {"type": "choice", "choice": "stuck_help", "confidence": 0.9,
             "probabilities": {"stuck_help": 0.9, "heads_up": 0.07, "none": 0.03}},
    "urgency": {"type": "score", "score": 3.0, "confidence": 0.7, "legend": {}, "probabilities": {}},
}}


def test_request_shape_and_answer_mapping():
    client, seen = fake_jev([(200, OK)])
    out = jev_engine("sk-test", client)("he is stuck", QS)
    body = json.loads(seen[0].content)
    assert seen[0].url == judge.JEV_URL and seen[0].headers["authorization"] == "Bearer sk-test"
    assert body["model"] == config.JEV_MODEL and body["state"] == "he is stuck"
    assert body["questions"]["speak"] == {"type": "noul", "instructions": "Should JARVIS speak?"}
    assert body["questions"]["kind"]["criteria"] == {"stuck_help": None, "heads_up": None, "none": None}
    assert body["questions"]["urgency"]["criteria"] == judge.DEFAULT_LEVELS
    assert out["speak"] == Answer(True, 0.82)
    assert out["kind"] == Answer("stuck_help", 0.9)
    assert out["urgency"].value == pytest.approx(0.75)


def test_low_noul_maps_to_confident_no():
    body = json.loads(json.dumps(OK))
    body["answers"]["speak"]["noul"] = 0.1
    client, _ = fake_jev([(200, body)])
    assert jev_engine("k", client)("x", QS)["speak"] == Answer(False, pytest.approx(0.9))


def test_retries_rate_limits_then_errors_without_leaking_key(monkeypatch):
    monkeypatch.setattr(judge.time, "sleep", lambda s: None)
    client, seen = fake_jev([(429, {}), (529, {}), (200, OK)])
    assert jev_engine("sk-secret", client)("x", QS)["speak"].value is True
    assert len(seen) == 3

    client, _ = fake_jev([(401, {"error": "invalid api key"})])
    with pytest.raises(JevError) as err:
        jev_engine("sk-secret", client)("x", QS)
    assert "401" in str(err.value) and "sk-secret" not in str(err.value)


def test_fallback_to_backup_engine():
    def broken(state, qs):
        raise JevError("Jev 529")
    notes = []
    decide = with_fallback(broken, lambda s, qs: {"speak": Answer(False, 0.9)}, notes.append)
    assert decide("x", QS[:1])["speak"].value is False and "529" in str(notes[0])


def test_engine_selection(monkeypatch):
    monkeypatch.setattr(config, "JUDGE_ENGINE", "auto")
    monkeypatch.setattr(config, "TYPESAFE_API_KEY", "")
    assert judge.fast_engine() is None
    monkeypatch.setattr(config, "TYPESAFE_API_KEY", "k")
    assert judge.fast_engine() is not None
    monkeypatch.setattr(config, "JUDGE_ENGINE", "claude")
    assert judge.fast_engine() is None


def routed(yes: set[str]):
    def fast(state, questions):
        return {q.key: Answer(q.key in yes, 0.9) for q in questions}
    return fast


def test_router_picks_blocks_and_falls_back_to_all():
    assert router.route("what's due?", [], None).blocks == router.ALL
    r = router.route("what's due?", [], routed({"deadlines"}))
    assert r.blocks == {"deadlines"} and not r.heavy and r.how == "jev"
    unsure = lambda s, qs: {q.key: Answer(False, 0.6) for q in qs}  # noqa: E731  (40% yes: include)
    assert router.route("hmm", [], unsure).blocks == router.ALL

    def broken(s, qs):
        raise JevError("down")
    errors = []
    r = router.route("x", [], broken, errors.append)
    assert r.blocks == router.ALL and r.how == "keywords" and errors


def test_jev_decides_whether_to_peek_at_the_screen(monkeypatch):
    monkeypatch.setattr(config, "PRE_PEEK", True)  # the pre-peek is opt-in
    yes = router.route("what's this error?", [], routed({"look_at_screen"}))
    assert yes.look and "help" in yes.look_why
    no = router.route("thanks", [], routed(set()))
    assert not no.look and "not needed" in no.look_why
    unsure = lambda s, qs: {q.key: Answer(False, 0.6) for q in qs}  # noqa: E731  (40% yes: look)
    r = router.route("hmm", [], unsure)
    assert r.look and "unsure" in r.look_why
    junk = lambda s, qs: {q.key: Answer("maybe" if q.key == "look_at_screen" else False, 0.9) for q in qs}  # noqa: E731
    r = router.route("hmm", [], junk)
    assert r.look and "couldn't read" in r.look_why
    missing = lambda s, qs: {q.key: Answer(False, 0.9) for q in qs if q.key != "look_at_screen"}  # noqa: E731
    assert router.route("hmm", [], missing).look

    def broken(s, qs):
        raise JevError("down")
    assert router.route("x", [], broken).look and router.route("x", [], None).look


def test_no_pre_peek_unless_turned_on(monkeypatch):
    monkeypatch.setattr(config, "PRE_PEEK", False)
    asked = []
    fast = lambda s, qs: asked.extend(q.key for q in qs) or {q.key: Answer(True, 0.9) for q in qs}  # noqa: E731
    r = router.route("what's this error?", [], fast)
    assert not r.look and "PRE_PEEK" in r.look_why and "look_at_screen" not in asked
    assert not router.route("x", [], None).look


def test_model_choice(monkeypatch):
    monkeypatch.setattr(config, "JARVIS_MODEL_ROUTING", "auto")
    assert router.route("draft an email to prof", [], routed({"heavy"})).heavy
    assert not router.route("thanks", [], routed(set())).heavy
    # without Jev: keywords and length
    assert router.route("why does this traceback happen?", [], None).heavy
    assert router.route("x" * 400, [], None).heavy
    assert not router.route("remind me at 9 to call mom", [], None).heavy
    monkeypatch.setattr(config, "JARVIS_MODEL_ROUTING", "heavy")
    assert router.route("thanks", [], routed(set())).heavy
    monkeypatch.setattr(config, "JARVIS_MODEL_ROUTING", "fast")
    assert not router.route("draft an email", [], routed({"heavy"})).heavy


def test_prompt_skips_unrouted_blocks():
    m = Memory(":memory:")
    now = datetime(2026, 10, 16, 9, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    m.save_journal("2026-10-15", "Built voice.", [])
    full = build_prompt(m, "hi", now, "Sun: Econ", inbox_text="INBOX-HEADERS", activity_text="SCREEN-TEXT")
    assert "INBOX-HEADERS" in full and "SCREEN-TEXT" in full and "Built voice." in full
    slim = build_prompt(m, "hi", now, "Sun: Econ", inbox_text="INBOX-HEADERS", activity_text="SCREEN-TEXT",
                        blocks=frozenset({"deadlines"}))
    assert "Sun: Econ" in slim and "INBOX-HEADERS" not in slim and "SCREEN-TEXT" not in slim
    assert "Built voice." not in slim and slim.count(SKIPPED) >= 4


def test_latency_summary():
    from jarvis.cli import latency_summary
    lines = ["2.1s total, first sentence 1.0s, route 0.20s (jev), context 0.01s, model haiku warm, blocks: journal",
             "3.1s total, first sentence 1.4s, route 0.20s (jev), context 0.50s, model haiku warm, blocks: deadlines",
             "7.0s total, first sentence -, route 0.20s (jev), context 0.10s, model sonnet cold, blocks: inbox"]
    out = latency_summary(lines)
    assert "3 recent replies" in out
    assert "haiku warm     n=2   median 2.6s, slowest 3.1s, first sentence median 1.2s" in out
    assert "sonnet cold    n=1   median 7.0s" in out
    assert latency_summary([]) == "No timed replies yet."
