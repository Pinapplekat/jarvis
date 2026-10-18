import json
import sys
from datetime import datetime, timedelta, timezone

import pytest

from jarvis import agent, config, reports, tasks, terminal
from jarvis.judge import Answer
from jarvis.memory import Memory

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS commands")


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    (root / "jarvis").mkdir(parents=True)
    (root / "jarvis" / "ears.py").write_text("THRESHOLD = 0.6\n")
    other = tmp_path / "site"
    other.mkdir()
    monkeypatch.setattr(config, "PROJECT_DIR", root)
    monkeypatch.setattr(terminal, "AUDIT", tmp_path / "commands.jsonl")
    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / "tasks")
    return root


class FakeTelegram:
    def __init__(self):
        self.sent, self.edits = [], []

    def send(self, text, buttons=None):
        self.sent.append((text, buttons))
        return len(self.sent)

    def edit(self, message_id, text):
        self.edits.append((message_id, text))


def session(*runs):
    """Claude Code stand-in: each run is (tool updates, things to tell Elijah, Outcome fields)."""
    calls = []

    def run(prompt, root, state, model, rules, resume=None, on_tool=None, on_tell=None, should_stop=None, **kw):
        calls.append({"prompt": prompt, "root": root, "resume": resume, "rules": rules, **kw})
        tools, tells, fields = runs[min(len(calls), len(runs)) - 1]
        for update in tools:
            on_tool(update)
        for text in tells:
            on_tell(text)
        if should_stop():
            return agent.Outcome(session_id="s1", stopped=True)
        return agent.Outcome(**{"session_id": "s1", **fields})
    run.calls = calls
    return run


def done(summary="Done, sir."):
    return {"text": f"Fixed it.\nSTATUS: done\n{summary}"}


def judge_says(met=True, tier=None):
    """A Jev stand-in: yes/no questions get `met`; a risk grade gets `tier` (low when met, else high)."""
    def decide(state, questions):
        out = {q.key: Answer(met, 0.9) for q in questions}
        if "risk" in out:
            out["risk"] = Answer(tier or ("low" if met else "high"), 0.9)
        return out
    return decide


def run_task(m, worker, goal="fix the failing test", folder=None):
    tid = tasks.create(m, goal, folder=folder)
    tasks.start_queued(m, worker, background=False)
    return tasks.get(m, tid)


def test_works_to_the_end_and_reports_to_jarvis_not_elijah(project, tmp_path):
    m, tg = Memory(":memory:"), FakeTelegram()
    run = session((["Reading app.py", "Running pytest -q", "Editing app.py"], [], done("All green now, sir.")))
    t = run_task(m, tasks.Worker(tg, runner=run, judge=judge_says(True), log=lambda s: None), folder=str(tmp_path / "site"))
    assert t["status"] == "done" and t["summary"] == "All green now, sir."
    assert [s["update"] for s in tasks.steps_of(t)] == ["Reading app.py", "Running pytest -q", "Editing app.py"]
    assert run.calls[0]["root"] == (tmp_path / "site").resolve() and run.calls[0]["edits"] is True
    assert "fix the failing test" in run.calls[0]["prompt"] and str(tmp_path / "site") in run.calls[0]["rules"]
    assert t["session_id"] == "s1"
    assert tg.sent == [] and tg.edits == []  # nothing from the task itself reaches his chat
    [filed] = reports.recent(m)
    assert (filed["kind"], filed["about"], filed["text"]) == ("done", "fix the failing test", "All green now, sir.")


def test_no_folder_means_jarvis_project_read_only(project):
    m, tg = Memory(":memory:"), FakeTelegram()
    run = session(([], [], done()))
    run_task(m, tasks.Worker(tg, runner=run, log=lambda s: None))
    assert run.calls[0]["root"] == project.resolve() and run.calls[0]["edits"] is False
    assert "read-only to you" in run.calls[0]["prompt"]


def test_telling_jarvis_something_is_a_step_not_the_end(project, tmp_path):
    m, tg = Memory(":memory:"), FakeTelegram()
    run = session((["Reading logs"], ["Found it, sir: the ears log is full. Clearing it now."], done("Cleared, sir.")))
    t = run_task(m, tasks.Worker(tg, runner=run, log=lambda s: None), folder=str(tmp_path / "site"))
    kinds = [(r["kind"], r["text"]) for r in reversed(reports.recent(m))]
    assert kinds == [("note", "Found it, sir: the ears log is full. Clearing it now."), ("done", "Cleared, sir.")]
    assert t["status"] == "done" and tg.sent == []  # it kept going after the note


def test_risky_step_pauses_asks_in_plain_words_then_resumes_with_the_output(project, tmp_path):
    m, tg = Memory(":memory:"), FakeTelegram()
    target = tmp_path / "old"
    (target / "x").mkdir(parents=True)
    blocked = {"tool": "Bash", "command": f"rm -r {target}", "why": "clear the old build", "cwd": str(tmp_path),
               "reason": "recursive rm outside the project", "risk": "critical"}
    run = session((["Running rm"], [], {"blocked": blocked}), ([], [], done("Gone, sir.")))
    spoken = []
    worker = tasks.Worker(tg, runner=run, log=lambda s: None, say=spoken.append)
    t = run_task(m, worker, folder=str(tmp_path / "site"))
    assert t["status"] == "waiting" and target.exists()
    request, buttons = next((x, b) for x, b in tg.sent if b)
    assert "for the task I'm on" in request and "Yes or no?" in request and f"rm -r {target}" in request
    assert "#" not in request.split("$")[0] and spoken  # no internal numbers; asked out loud too

    def carry_on(db, payload, action_id, outcome):
        if tasks.requeue(db, payload, action_id, outcome):
            tasks.start_queued(db, worker, background=False)
    terminal.handle_button(m, tg, "cmd_run", t["waiting_on"], background=False, on_done=carry_on)
    t = tasks.get(m, t["id"])
    assert t["status"] == "done" and not target.exists()
    assert run.calls[1]["resume"] == "s1" and "Elijah said yes" in run.calls[1]["prompt"]
    assert "exit 0" in run.calls[1]["prompt"]
    assert not any(": $ rm" in text for text, _ in tg.sent)  # the output went to the session, not a message


def test_rejection_is_reported_back_to_the_session(project, tmp_path):
    m, tg = Memory(":memory:"), FakeTelegram()
    blocked = {"tool": "Bash", "command": "git push", "cwd": str(tmp_path), "reason": "git push", "risk": "high"}
    run = session(([], [], {"blocked": blocked}), ([], [], {"text": "STATUS: stuck\nHe said no, sir."}))
    worker = tasks.Worker(tg, runner=run, log=lambda s: None)
    t = run_task(m, worker, folder=str(tmp_path / "site"))
    terminal.handle_button(m, tg, "cmd_reject", t["waiting_on"], background=False,
                           on_done=lambda db, p, aid, out: tasks.requeue(db, p, aid, out)
                           and tasks.start_queued(db, worker, background=False))
    assert "did not approve `git push`" in run.calls[-1]["prompt"]
    assert tasks.get(m, t["id"])["status"] == "failed" and reports.recent(m)[0]["text"] == "He said no, sir."


def test_a_doubtful_done_gets_one_more_look(project, tmp_path):
    m, tg = Memory(":memory:"), FakeTelegram()
    run = session(([], [], done("Done.")), ([], [], done("Verified, sir.")))
    t = run_task(m, tasks.Worker(tg, runner=run, judge=judge_says(False), log=lambda s: None),
                 folder=str(tmp_path / "site"))
    assert "isn't convinced" in run.calls[1]["prompt"] and t["summary"] == "Verified, sir."


def test_unfinished_runs_continue_until_the_budget(project, monkeypatch, tmp_path):
    monkeypatch.setattr(tasks, "MAX_RUNS", 3)
    m, tg = Memory(":memory:"), FakeTelegram()
    run = session((["Looking around"], [], {"text": "still going"}))
    t = run_task(m, tasks.Worker(tg, runner=run, log=lambda s: None), folder=str(tmp_path / "site"))
    assert len(run.calls) == 3 and "step limit" in run.calls[1]["prompt"]
    assert t["status"] == "failed" and "ran out of budget (3 runs)" in t["summary"]


def test_a_hang_resumes_the_same_session(project, tmp_path):
    m, tg = Memory(":memory:"), FakeTelegram()
    run = session(([], [], {"hung": True}), ([], [], done()))
    run_task(m, tasks.Worker(tg, runner=run, log=lambda s: None), folder=str(tmp_path / "site"))
    assert run.calls[1]["resume"] == "s1" and "went quiet" in run.calls[1]["prompt"]


def test_a_crash_ends_the_task_and_says_so(project):
    m, tg = Memory(":memory:"), FakeTelegram()

    def broken(*a, **k):
        raise RuntimeError("Claude CLI failed")
    t = run_task(m, tasks.Worker(tg, runner=broken, log=lambda s: None))
    assert t["status"] == "failed" and "internal error" in reports.recent(m)[0]["text"]


def test_stop_cancels_the_waiting_command(project, tmp_path):
    m, tg = Memory(":memory:"), FakeTelegram()
    blocked = {"tool": "Bash", "command": "git push", "cwd": str(tmp_path), "reason": "", "risk": "high"}
    t = run_task(m, tasks.Worker(tg, runner=session(([], [], {"blocked": blocked})), log=lambda s: None),
                 folder=str(tmp_path / "site"))
    assert tasks.parse_stop("stop") == (True, None) and tasks.parse_stop("Stop task #3.") == (True, 3)
    assert tasks.parse_stop("stop worrying about it") == (False, None)
    assert tasks.stop(m) == "Stopped the task (fix the failing test), sir."
    assert m.get_action(t["waiting_on"])["status"] == "rejected"
    assert tasks.stop(m) == "No task is running, sir."


def test_stop_while_running_kills_the_session(project, tmp_path):
    m, tg = Memory(":memory:"), FakeTelegram()
    holder = {}

    def run(prompt, root, state, model, rules, on_tool=None, should_stop=None, **kw):
        tasks.stop(m, holder["id"])  # Elijah says stop mid-run
        return agent.Outcome(session_id="s1", stopped=should_stop())
    tid = tasks.create(m, "long job", folder=str(tmp_path / "site"))
    holder["id"] = tid
    tasks.start_queued(m, tasks.Worker(tg, runner=run, log=lambda s: None), background=False)
    assert tasks.get(m, tid)["status"] == "stopped"


def test_expired_approval_requeues_the_task(project, tmp_path):
    m, tg = Memory(":memory:"), FakeTelegram()
    blocked = {"tool": "Bash", "command": "git push", "cwd": str(tmp_path), "reason": "", "risk": "high"}
    t = run_task(m, tasks.Worker(tg, runner=session(([], [], {"blocked": blocked})), log=lambda s: None),
                 folder=str(tmp_path / "site"))
    late = datetime.now(timezone.utc) + timedelta(minutes=config.COMMAND_APPROVAL_MINUTES + 1)
    terminal.expire_commands(m, late, on_expired=lambda aid, p: tasks.requeue(
        m, p, aid, "(the approval request expired before Elijah answered)"))
    t = tasks.get(m, t["id"])
    assert t["status"] == "queued" and "expired" in t["resume_note"]


def test_restart_recovery_resumes_the_session_and_chat_context(project, tmp_path):
    m = Memory(":memory:")
    tid = tasks.create(m, "fix the tests")
    tasks._save(m, tid, status="running", session_id="abc", steps=[{"n": 1, "update": "Running tests", "ok": None}])
    assert tasks.recover(m) == 1
    t = tasks.get(m, tid)
    assert t["status"] == "queued" and "restarted" in t["resume_note"]
    terminal.request_approval(m, terminal.classify(f"touch {tmp_path}/x"), None, "")
    text = tasks.for_prompt(m)
    assert f"task #{tid} [queued, 1 steps, 0 min] fix the tests" in text and "[about to be sent for OK]" in text


def test_folders_that_are_too_broad_are_refused(project, tmp_path):
    from pathlib import Path
    for bad in ["/", str(Path.home()), str(Path.home() / ".ssh"), str(tmp_path / "nope")]:
        with pytest.raises(ValueError):
            tasks.check_folder(bad)
    assert tasks.check_folder(str(tmp_path / "site")) == (tmp_path / "site").resolve()


def test_kill_switch(project, monkeypatch):
    monkeypatch.setattr(config, "TASKS", False)
    m = Memory(":memory:")
    tasks.create(m, "anything")
    assert tasks.start_queued(m, tasks.Worker(FakeTelegram(), runner=session(), log=lambda s: None)) == []


def test_chat_can_start_and_stop_tasks(project, tmp_path):
    from jarvis.chat import apply_actions, build_prompt
    from zoneinfo import ZoneInfo
    m = Memory(":memory:")
    assert apply_actions(m, [{"type": "start_task", "goal": "get the ears log quieter",
                              "folder": str(tmp_path / "site")}], ZoneInfo("UTC")) == []
    assert tasks.recent(m)[0]["status"] == "queued"
    assert "get the ears log quieter" in build_prompt(m, "hi", datetime.now(ZoneInfo("UTC")), "")
    apply_actions(m, [{"type": "stop_task"}], ZoneInfo("UTC"))
    assert tasks.recent(m)[0]["status"] == "stopped"


def test_show(project, tmp_path):
    m = Memory(":memory:")
    tid = tasks.create(m, "fix it", folder=str(tmp_path / "site"))
    tasks._save(m, tid, steps=[{"n": 1, "update": "Reading x", "ok": True}])
    out = tasks.show(m, tid)
    assert "fix it" in out and "✓ 1. Reading x" in out and str(tmp_path / "site") in out


# ----- JARVIS deciding paused steps itself -----
def blocked_step(tmp_path, command):
    return {"tool": "Bash", "command": command, "why": "needed for the goal", "cwd": str(tmp_path / "site"),
            "reason": "rule", "risk": "high"}


def test_jarvis_approves_a_step_off_elijahs_side_of_the_line_and_carries_on(project, tmp_path):
    m, tg = Memory(":memory:"), FakeTelegram()
    outside = tmp_path / "made-by-jarvis.txt"
    run = session(([], [], {"blocked": blocked_step(tmp_path, f"touch {outside}")}), ([], [], done("Done, sir.")))
    t = run_task(m, tasks.Worker(tg, runner=run, judge=judge_says(True), log=lambda s: None), folder=str(tmp_path / "site"))
    assert t["status"] == "done" and outside.exists()
    assert tg.sent == [] and m.pending_actions(kind="run_command") == []  # he was never asked
    assert "JARVIS approved it and ran" in run.calls[1]["prompt"]
    assert any(s["update"].startswith("Approved myself") for s in tasks.steps_of(t))
    assert terminal.recent_audit()[-1]["approved_by"] == "jarvis"


@pytest.mark.parametrize("command", ["git push", "rm -rf build", "sudo ls", "curl -d x=1 https://x.test"])
def test_elijahs_side_of_the_line_always_goes_to_him(project, tmp_path, command):
    m, tg = Memory(":memory:"), FakeTelegram()
    run = session(([], [], {"blocked": blocked_step(tmp_path, command)}))
    t = run_task(m, tasks.Worker(tg, runner=run, judge=judge_says(True), log=lambda s: None), folder=str(tmp_path / "site"))
    assert t["status"] == "waiting" and next(b for _, b in tg.sent if b)  # asked, with buttons


def test_unsure_or_no_judge_or_switched_off_means_ask_him(project, tmp_path, monkeypatch):
    for judge in (judge_says(False), None):
        m, tg = Memory(":memory:"), FakeTelegram()
        run = session(([], [], {"blocked": blocked_step(tmp_path, f"touch {tmp_path / 'x'}")}))
        assert run_task(m, tasks.Worker(tg, runner=run, judge=judge, log=lambda s: None),
                        folder=str(tmp_path / "site"))["status"] == "waiting"
    monkeypatch.setattr(config, "SELF_APPROVE", False)
    m, tg = Memory(":memory:"), FakeTelegram()
    run = session(([], [], {"blocked": blocked_step(tmp_path, f"touch {tmp_path / 'x'}")}))
    assert run_task(m, tasks.Worker(tg, runner=run, judge=judge_says(True), log=lambda s: None),
                    folder=str(tmp_path / "site"))["status"] == "waiting"


def test_review_treats_a_judge_error_as_ask_him(project, tmp_path):
    def broken(state, questions):
        raise RuntimeError("Jev down")
    ok, why = agent.review(broken, "goal", blocked_step(tmp_path, "brew install jq"), tmp_path / "site")
    assert not ok and "couldn't grade" in why


def test_chat_sees_where_an_active_task_is(project, tmp_path):
    m = Memory(":memory:")
    tid = tasks.create(m, "fix the site tests", folder=str(tmp_path / "site"))
    tasks._save(m, tid, status="running", steps=[{"n": i, "update": f"step {i}", "ok": True} for i in range(1, 6)])
    assert "recent steps: step 3; step 4; step 5" in tasks.for_prompt(m)


def test_he_can_ask_to_see_the_steps(project, tmp_path):
    from jarvis.chat import respond
    from zoneinfo import ZoneInfo
    m = Memory(":memory:")
    tid = tasks.create(m, "fix the site tests", folder=str(tmp_path / "site"))
    tasks._save(m, tid, steps=[{"n": 1, "update": "Running pytest -q", "ok": True}])
    rounds = [{"reply": "checking", "actions": [{"type": "task_detail"}]}, {"reply": "One step so far, sir.", "actions": []}]
    prompts = []

    def run(system, prompt, on_text=None):
        prompts.append(prompt)
        return rounds[min(len(prompts), 2) - 1]
    assert respond(m, "show me the steps", datetime.now(ZoneInfo("UTC")), "", runner=run) == "One step so far, sir."
    assert "✓ 1. Running pytest -q" in prompts[1] and "<helpers_reports" in prompts[0]


def test_images_the_session_attaches_ride_along_with_the_report(project, tmp_path):
    m, tg = Memory(":memory:"), FakeTelegram()
    shot = str(tmp_path / "site" / "chart.png")
    run = session(([], [], {"text": f"Made it.\nATTACH: {shot}\nSTATUS: done\nThe chart's ready, sir.",
                            "attachments": [shot]}))
    t = run_task(m, tasks.Worker(tg, runner=run, log=lambda s: None), folder=str(tmp_path / "site"))
    filed = reports.recent(m)[0]
    assert t["summary"] == "The chart's ready, sir." and json.loads(filed["attachments"]) == [shot]


def test_attach_lines_are_read_from_the_stream():
    out = agent.Outcome(text="Done.\nATTACH: /x/a.png\nSTATUS: done\nAll set, sir.")
    assert out.summary == "All set, sir." and agent.attach_lines(out.text) == ["/x/a.png"]


def test_jev_grade_decides_off_the_line_and_failure_means_ask(project, tmp_path):
    step = blocked_step(tmp_path, f"touch {tmp_path / 'x'}")
    assert agent.review(judge_says(True, tier="medium"), "goal", step, tmp_path / "site")[0] is True
    assert agent.review(judge_says(True, tier="high"), "goal", step, tmp_path / "site") == (False, "graded high risk")
    junk = lambda state, qs: {q.key: Answer("maybe", 0.9) for q in qs}  # noqa: E731
    assert agent.review(junk, "goal", step, tmp_path / "site") == (False, "couldn't grade it")


def test_low_risk_but_not_what_he_asked_for_still_asks(project, tmp_path):
    def sneaky(state, questions):
        return {"risk": Answer("low", 0.95), "serves_goal": Answer(False, 0.9)}
    ok, why = agent.review(sneaky, "fix the tests", blocked_step(tmp_path, f"touch {tmp_path / 'y'}"), tmp_path / "site")
    assert not ok and "not clearly something you asked for" in why


def test_the_final_report_says_what_jarvis_ran_on_its_own(project, tmp_path):
    m, tg = Memory(":memory:"), FakeTelegram()
    run = session(([], [], {"blocked": blocked_step(tmp_path, f"touch {tmp_path / 'z'}")}), ([], [], done("Done, sir.")))
    run_task(m, tasks.Worker(tg, runner=run, judge=judge_says(True), log=lambda s: None), folder=str(tmp_path / "site"))
    assert "Ran on my own along the way: touch" in reports.recent(m)[0]["text"]
