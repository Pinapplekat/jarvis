import json
import sys
from datetime import datetime, timedelta, timezone

import pytest

from jarvis import config, tasks, terminal
from jarvis.judge import Answer
from jarvis.memory import Memory

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS commands")


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    (root / "jarvis").mkdir(parents=True)
    (root / "jarvis" / "ears.py").write_text("THRESHOLD = 0.6\n")
    monkeypatch.setattr(config, "PROJECT_DIR", root)
    monkeypatch.setattr(terminal, "AUDIT", tmp_path / "commands.jsonl")
    return root


class FakeTelegram:
    def __init__(self):
        self.sent, self.edits = [], []

    def send(self, text, buttons=None):
        self.sent.append((text, buttons))
        return len(self.sent)

    def edit(self, message_id, text):
        self.edits.append((message_id, text))


def scripted(*steps):
    """Claude stand-in: returns each scripted step in turn, recording the prompts it saw."""
    prompts = []

    def think(system, prompt):
        prompts.append(prompt)
        return steps[min(len(prompts), len(steps)) - 1]
    think.prompts = prompts
    return think


def step(update, **action):
    return {"thought": "", "update": update, "action": action}


def judge_says(stuck=False, met=False, risk=0.0):
    calls = []

    def decide(state, questions):
        calls.append([q.key for q in questions])
        out = {"step_ok": Answer(True, 0.9), "goal_met": Answer(met, 0.9), "stuck": Answer(stuck, 0.9),
               "risk": Answer(risk, 0.9)}
        return {q.key: out[q.key] for q in questions}
    decide.calls = calls
    return decide


def run_task(m, worker, goal="find the wake threshold"):
    tid = tasks.create(m, goal)
    tasks.start_queued(m, worker, background=False)
    return tasks.get(m, tid)


def test_steps_until_finished_with_live_progress(project):
    m, tg = Memory(":memory:"), FakeTelegram()
    think = scripted(step("Searching the code", type="inspect", op="search", arg="THRESHOLD"),
                     step("Counting lines", type="run_command", command="wc -l jarvis/ears.py"),
                     step("Done", type="finish", success=True, summary="It's 0.6, sir."))
    t = run_task(m, tasks.Worker(tg, think=think, judge=judge_says(), log=lambda s: None))
    assert t["status"] == "done" and t["summary"] == "It's 0.6, sir."
    steps = tasks.steps_of(t)
    assert [s["update"] for s in steps] == ["Searching the code", "Counting lines"]
    assert "jarvis/ears.py:1:" in steps[0]["result"] and "exit 0" in steps[1]["result"]
    assert "checker: last step worked" in think.prompts[2]  # the judge's note reaches Claude
    assert "untrusted data" in think.prompts[1]
    assert tg.sent[0][0].startswith("Task #1: find the wake threshold")  # one progress message...
    assert tg.edits and "✅ done" in tg.edits[-1][1]                      # ...kept up to date
    assert tg.sent[-1][0] == "It's 0.6, sir."


def test_risky_step_pauses_for_approval_then_resumes_with_the_output(project):
    m, tg = Memory(":memory:"), FakeTelegram()
    target = project / "old.log"
    target.write_text("x")
    think = scripted(step("Deleting the old log", type="run_command", command=f"rm {target}", why="he asked"),
                     step("Checking it's gone", type="run_command", command="ls"),
                     step("Done", type="finish", success=True, summary="Deleted, sir."))
    worker = tasks.Worker(tg, think=think, judge=judge_says(risk=0.8), log=lambda s: None)
    t = run_task(m, worker)
    assert t["status"] == "waiting" and target.exists()
    request, buttons = next((x, b) for x, b in tg.sent if b)
    assert "for task #1" in request and "⚠️ risk: high" in request and f"rm {target}" in request

    def carry_on(db, payload, action_id, outcome):
        if tasks.requeue(db, payload, action_id, outcome):
            tasks.start_queued(db, worker, background=False)
    terminal.handle_button(m, tg, "cmd_run", t["waiting_on"], background=False, on_done=carry_on)
    t = tasks.get(m, t["id"])
    assert t["status"] == "done" and not target.exists()
    assert "exit 0" in tasks.steps_of(t)[0]["result"]
    assert "old.log" not in tasks.steps_of(t)[1]["result"]
    assert not any(": $ rm" in text for text, _ in tg.sent)  # output lives in the task, not a separate message


def test_rejection_is_reported_back_to_claude(project):
    m, tg = Memory(":memory:"), FakeTelegram()
    think = scripted(step("Installing", type="run_command", command="uv add requests"),
                     step("Giving up", type="finish", success=False, summary="He said no."))
    worker = tasks.Worker(tg, think=think, log=lambda s: None)
    t = run_task(m, worker)
    terminal.handle_button(m, tg, "cmd_reject", t["waiting_on"], background=False,
                           on_done=lambda db, p, aid, out: tasks.requeue(db, p, aid, out)
                           and tasks.start_queued(db, worker, background=False))
    assert "Elijah rejected" in think.prompts[-1] and tasks.get(m, t["id"])["status"] == "failed"


def test_going_in_circles_gets_a_nudge_then_stops(project):
    m, tg = Memory(":memory:"), FakeTelegram()
    think = scripted(step("Listing again", type="run_command", command="ls"))
    t = run_task(m, tasks.Worker(tg, think=think, judge=judge_says(stuck=True), log=lambda s: None))
    assert "genuinely different approach" in think.prompts[tasks.STUCK_NUDGE]
    assert t["status"] == "failed" and "going in circles" in t["summary"]
    assert len(tasks.steps_of(t)) == tasks.STUCK_STOP


def test_step_budget(project, monkeypatch):
    monkeypatch.setattr(tasks, "MAX_STEPS", 2)
    m, tg = Memory(":memory:"), FakeTelegram()
    t = run_task(m, tasks.Worker(tg, think=scripted(step("Looking", type="run_command", command="pwd")),
                                 log=lambda s: None))
    assert t["status"] == "failed" and "ran out of budget (2 steps)" in t["summary"]


def test_refused_and_unknown_actions_become_results_not_crashes(project):
    m, tg = Memory(":memory:"), FakeTelegram()
    think = scripted(step("Piping", type="run_command", command="ls | wc"),
                     step("Reading a secret", type="inspect", op="read", arg="../../etc/passwd"),
                     step("Magic", type="teleport"),
                     step("Done", type="finish", success=True, summary="ok"))
    t = run_task(m, tasks.Worker(tg, think=think, log=lambda s: None))
    results = [s["result"] for s in tasks.steps_of(t)]
    assert "shell syntax" in results[0] and "outside" in results[1] and "unknown action" in results[2]
    assert t["status"] == "done"


def test_a_crash_ends_the_task_and_says_so(project):
    m, tg = Memory(":memory:"), FakeTelegram()

    def broken(system, prompt):
        raise RuntimeError("Claude CLI failed")
    t = run_task(m, tasks.Worker(tg, think=broken, log=lambda s: None))
    assert t["status"] == "failed" and "internal error" in tg.sent[-1][0]


def test_stop_cancels_the_waiting_command(project):
    m, tg = Memory(":memory:"), FakeTelegram()
    t = run_task(m, tasks.Worker(tg, think=scripted(step("Deleting", type="run_command", command="rm x")),
                                 log=lambda s: None))
    assert tasks.parse_stop("stop") == (True, None) and tasks.parse_stop("Stop task #3.") == (True, 3)
    assert tasks.parse_stop("stop worrying about it") == (False, None)
    assert tasks.stop(m) == f"Stopped task #{t['id']}, sir."
    assert m.get_action(t["waiting_on"])["status"] == "rejected"
    assert tasks.stop(m) == "No task is running, sir."


def test_expired_approval_requeues_the_task(project):
    m, tg = Memory(":memory:"), FakeTelegram()
    t = run_task(m, tasks.Worker(tg, think=scripted(step("Deleting", type="run_command", command="rm x")),
                                 log=lambda s: None))
    late = datetime.now(timezone.utc) + timedelta(minutes=config.COMMAND_APPROVAL_MINUTES + 1)
    terminal.expire_commands(m, late, on_expired=lambda aid, p: tasks.requeue(m, p, aid, "(the approval request expired)"))
    t = tasks.get(m, t["id"])
    assert t["status"] == "queued" and "expired" in tasks.steps_of(t)[0]["result"]


def test_restart_recovery_and_chat_context(project):
    m = Memory(":memory:")
    tid = tasks.create(m, "fix the tests")
    tasks._save(m, tid, status="running", steps=[{"n": 1, "update": "Running tests", "action": {}, "result": "x"}])
    assert tasks.recover(m) == 1 and tasks.get(m, tid)["status"] == "queued"
    terminal.request_approval(m, terminal.classify("rm x"), None, "")
    text = tasks.for_prompt(m)
    assert f"task #{tid} [queued, 1 steps] fix the tests" in text and "command #1 [about to be sent for OK] $ rm x" in text


def test_chat_can_start_and_stop_tasks(project):
    from jarvis.chat import apply_actions, build_prompt
    from zoneinfo import ZoneInfo
    m = Memory(":memory:")
    assert apply_actions(m, [{"type": "start_task", "goal": "get the ears log quieter"}], ZoneInfo("UTC")) == []
    assert tasks.recent(m)[0]["status"] == "queued"
    assert "get the ears log quieter" in build_prompt(m, "hi", datetime.now(ZoneInfo("UTC")), "")
    apply_actions(m, [{"type": "stop_task"}], ZoneInfo("UTC"))
    assert tasks.recent(m)[0]["status"] == "stopped"


def test_new_lookups_run_automatically(project):
    for command in ["which git", "pgrep -fl python", "ps -ax", "find . -name ears.py", "git --version"]:
        assert terminal.classify(command).kind == "auto", command
    for command in ["find . -delete", "find / -name x", "find . -exec rm {} ;"]:
        assert terminal.classify(command).kind != "auto", command
    assert json.loads(json.dumps(terminal.classify("which git").argv)) == ["which", "git"]
