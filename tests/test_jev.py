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
    assert router.route("what's due?", [], None) == router.ALL
    assert router.route("what's due?", [], routed({"deadlines"})) == {"deadlines"}
    unsure = lambda s, qs: {q.key: Answer(False, 0.6) for q in qs}  # noqa: E731  (40% yes: include)
    assert router.route("hmm", [], unsure) == router.ALL

    def broken(s, qs):
        raise JevError("down")
    errors = []
    assert router.route("x", [], broken, errors.append) == router.ALL and errors


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
