from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from jarvis import plans
from jarvis.chat import apply_actions
from jarvis.memory import Memory

TZ = ZoneInfo("America/Los_Angeles")
EVENING = datetime(2026, 10, 15, 19, 0, tzinfo=TZ)
PLAN = {"title": "Status command", "why": "see reminders fast", "steps": ["add /status", "test it"],
        "files": ["jarvis/cli.py"], "risks": "none really"}


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def send(self, text, buttons=None):
        self.sent.append((text, buttons))


@pytest.fixture(autouse=True)
def plans_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(plans, "PLANS_DIR", tmp_path / "plans")
    return tmp_path / "plans"


def runner_returning(out):
    calls = []

    def run(system, prompt):
        calls.append(prompt)
        return out
    run.calls = calls
    return run


def test_chat_request_becomes_a_drafted_plan_with_buttons():
    m, tg = Memory(":memory:"), FakeTelegram()
    assert apply_actions(m, [{"type": "plan_improvement", "origin": "request", "title": "status",
                              "request": "add a /status command"}], TZ) == []
    run = runner_returning(PLAN)
    assert plans.draft_plans(m, tg, run) == 1
    assert "add a /status command" in run.calls[0] and "jarvis/cli.py" in run.calls[0]
    text, buttons = tg.sent[0]
    assert text.startswith("📝 Plan: Status command") and "1. add /status" in text
    assert buttons == [("Yes, build it ✅", "plan_yes:1"), ("Not now", "plan_no:1")]
    assert m.get_self_edit(1)["status"] == "proposed"
    assert plans.draft_plans(m, tg, run) == 0


def test_yes_saves_plan_and_waits_for_claude_code_when_self_edit_off(plans_dir):
    m, tg = Memory(":memory:"), FakeTelegram()
    m.add_self_edit("request", "status", "add /status")
    plans.draft_plans(m, tg, runner_returning(PLAN))
    assert plans.handle_button(m, "plan_yes", 1) == "Approved, saved for Claude Code"
    assert plans.handle_button(m, "plan_yes", 1) == "Already handled"
    assert m.get_self_edit(1)["status"] == "approved"
    (saved,) = plans_dir.glob("*.md")
    assert saved.name == "001-status-command.md" and "add /status" in saved.read_text()


def test_yes_hands_off_to_builder_when_enabled():
    m, tg = Memory(":memory:"), FakeTelegram()
    m.add_self_edit("request", "status", "add /status")
    plans.draft_plans(m, tg, runner_returning(PLAN))
    handed = []
    assert plans.handle_button(m, "plan_yes", 1, on_approved=lambda i: handed.append(i) or "Building") == "Building"
    assert handed == [1]


def test_not_now_and_revise():
    m, tg = Memory(":memory:"), FakeTelegram()
    m.add_self_edit("idea", "a", "do a")
    m.add_self_edit("request", "b", "do b")
    plans.draft_plans(m, tg, runner_returning(PLAN))
    assert plans.handle_button(m, "plan_no", 1) == "Skipped"
    assert apply_actions(m, [{"type": "revise_plan", "id": 2, "feedback": "also show deadlines"}], TZ) == []
    assert m.get_self_edit(2)["status"] == "drafting" and "also show deadlines" in m.get_self_edit(2)["request"]
    assert apply_actions(m, [{"type": "revise_plan", "id": 1, "feedback": "x"}], TZ)  # declined: can't revise
    assert tg.sent[0][0].startswith("💡 Idea: ")


def test_planning_failure_is_reported():
    m, tg = Memory(":memory:"), FakeTelegram()
    m.add_self_edit("request", "x", "y")

    def broken(system, prompt):
        raise RuntimeError("usage limit")
    plans.draft_plans(m, tg, broken)
    assert m.get_self_edit(1)["status"] == "failed" and "usage limit" in tg.sent[0][0]


def test_bad_plan_actions_rejected():
    m = Memory(":memory:")
    problems = apply_actions(m, [
        {"type": "plan_improvement", "origin": "whim", "title": "t", "request": "r"},
        {"type": "plan_improvement", "origin": "idea", "title": "", "request": "r"},
    ], TZ)
    assert len(problems) == 2 and m.self_edits() == []


def test_daily_idea_once_in_evening_and_not_piling_up():
    m = Memory(":memory:")
    run = runner_returning({"idea": {"title": "Photo support", "request": "read shift screenshots"}})
    assert plans.maybe_propose_idea(m, EVENING.replace(hour=9), run) is None
    first = plans.maybe_propose_idea(m, EVENING, run)
    assert first and m.get_self_edit(first)["status"] == "drafting"
    assert plans.maybe_propose_idea(m, EVENING.replace(hour=22), run) is None
    m.add_self_edit("idea", "another", "x")
    m.set_kv("last_idea_date", "2000-01-01")
    assert plans.maybe_propose_idea(m, EVENING, run) is None
    assert len(run.calls) == 1


def test_combined_plan_is_marked_built_once_its_host_deploys(monkeypatch):
    m = Memory(":memory:")
    absorbed = m.add_self_edit("request", "finish sentences", "x")
    host = m.add_self_edit("request", "barge-in", "y")
    monkeypatch.setattr(plans, "COMBINED", {absorbed: host})
    m.set_self_edit(absorbed, status="approved")
    m.set_self_edit(host, status="ready")
    assert plans.settle_combined(m) == []  # not deployed yet: leave it
    m.set_self_edit(host, status="deployed")
    assert plans.settle_combined(m) == [absorbed]
    job = m.get_self_edit(absorbed)
    assert job["status"] == "deployed" and "barge-in" in job["summary"]
    assert plans.settle_combined(m) == []


def test_prod_mode_replaces_the_daily_idea(monkeypatch):
    from jarvis import prodmode
    m = Memory(":memory:")
    started = []
    monkeypatch.setattr(prodmode, "enabled", lambda: True)
    monkeypatch.setattr(prodmode, "maybe_start", lambda mem, now, tg=None: started.append((now, tg)) or "not due")
    run = runner_returning({"idea": {"title": "Photo support", "request": "read shift screenshots"}})
    assert plans.maybe_propose_idea(m, EVENING, run, tg="tg") is None
    assert run.calls == [] and m.self_edits() == [] and started == [(EVENING, "tg")]


def test_no_idea_is_fine():
    m = Memory(":memory:")
    assert plans.maybe_propose_idea(m, EVENING, runner_returning({"idea": None})) is None
    assert m.self_edits() == []
