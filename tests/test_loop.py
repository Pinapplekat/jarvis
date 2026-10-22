"""The agent loop for chat turns (jarvis/loop.py), with a scripted chat model and a mocked Jev."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from jarvis import chat, config, judge, loop, reports, tasks, terminal
from jarvis.judge import Answer
from jarvis.memory import Memory

NOW = datetime(2026, 10, 19, 9, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
SOON = f"{datetime.now(NOW.tzinfo) + timedelta(days=1):%Y-%m-%d %H:%M}"  # reminders must be in the future


@pytest.fixture(autouse=True)
def agent_loop(monkeypatch):
    monkeypatch.setattr(config, "AGENT_LOOP", True)


def scripted(*rounds):
    """A chat model that answers each round in turn (repeating the last) and records the prompts."""
    prompts = []

    def run(system, prompt, **kw):
        prompts.append(prompt)
        return rounds[min(len(prompts), len(rounds)) - 1]
    run.prompts = prompts
    return run


def jev(*picks, calls=None):
    """Jev answering each next-step question in turn: (choice, confidence)."""
    asked = calls if calls is not None else []

    def decide(state, questions):
        asked.append(state)
        choice, conf = picks[min(len(asked), len(picks)) - 1]
        return {"next": Answer(choice, conf)}
    return decide


@pytest.fixture
def no_claude_judge(monkeypatch):
    """Claude as the backup judge must not be reached unless a test says so."""
    monkeypatch.setattr(judge, "engine", lambda *a, **k: pytest.fail("Claude judge called"))


def use_jev(monkeypatch, *picks):
    calls = []
    monkeypatch.setattr(judge, "fast_engine", lambda: jev(*picks, calls=calls))
    return calls


def events(m, kind):
    return [e["content"] for e in m.recent_events(200) if e["kind"] == kind]


@pytest.fixture
def screen(tmp_path, monkeypatch):
    from jarvis import screen as screen_mod
    shot = tmp_path / "vision.jpg"

    def capture(run=None):
        shot.write_bytes(b"\xff\xd8JPEG")
        return shot
    monkeypatch.setattr(screen_mod, "capture_for_vision", capture)
    return shot


@pytest.fixture
def commands(monkeypatch):
    """terminal.classify / run_command stand-ins: 'ls' runs, 'rm' needs his yes, 'bad' is refused."""
    ran = []

    def classify(command, cwd=None, root=None):
        argv = str(command).split()
        if argv[0] == "ls":
            return terminal.Verdict("auto", "reads", argv, risk="low")
        if argv[0] == "bad":
            return terminal.Verdict("reject", "shell syntax isn't allowed", argv)
        return terminal.Verdict("approval", "deletes files", argv, risk="high", yours="irreversible")

    def run_command(verdict, cwd=None, approved_by="auto"):
        ran.append(verdict.argv)
        return SimpleNamespace(code=0, output="notes.txt\nplan.md", timed_out=False)
    monkeypatch.setattr(terminal, "classify", classify)
    monkeypatch.setattr(terminal, "run_command", run_command)
    from jarvis import agent
    monkeypatch.setattr(agent, "review", lambda judge, goal, step, root, asker="": (False, "his side of the line"))
    return ran


# ----- the loop -----
def test_a_look_is_a_finding_and_another_action_follows_in_the_same_turn(monkeypatch, screen, no_claude_judge):
    m = Memory(":memory:")
    use_jev(monkeypatch, ("continue", 0.9), ("respond", 0.9))
    run = scripted({"reply": "Having a look, sir.", "actions": [{"type": "look_at_screen", "question": "the error"}]},
                   {"reply": "It's a KeyError on line 40; I'll remind you at ten to fix it.",
                    "actions": [{"type": "remind", "at": SOON, "text": "Fix the KeyError, sir."}]})
    vision = lambda system, prompt, image: "A KeyError in chat.py, line 40."  # noqa: E731
    reply = chat.respond(m, "what's this error? remind me at 10 to fix it", NOW, "", runner=run, vision=vision,
                         channel="telegram")
    assert "Having a look" not in reply and "KeyError on line 40" in reply  # the look didn't end the turn
    assert "A KeyError in chat.py, line 40." in run.prompts[1]  # it came back as a finding
    assert len(m.upcoming_reminders()) == 1 and not screen.exists()
    assert len(run.prompts) == 2


def test_command_output_is_used_in_the_next_round(monkeypatch, commands, no_claude_judge):
    m = Memory(":memory:")
    use_jev(monkeypatch, ("continue", 0.95))
    run = scripted({"reply": "Checking.", "actions": [{"type": "run_command", "command": "ls notes"}]},
                   {"reply": "Two files, sir: notes and a plan.", "actions": []})
    reply = chat.respond(m, "what's in notes?", NOW, "", runner=run, channel="telegram")
    assert reply == "Two files, sir: notes and a plan."
    assert "plan.md" in run.prompts[1] and "1. run_command" in run.prompts[1]  # output and the turn log
    assert commands == [["ls", "notes"]]


def test_a_command_needing_his_yes_parks_the_turn_then_resumes_with_the_result(monkeypatch, commands, no_claude_judge):
    m = Memory(":memory:")
    run = scripted({"reply": "That needs your OK, sir.", "actions": [{"type": "run_command", "command": "rm old.log"}]})
    reply = chat.respond(m, "delete old.log", NOW, "", runner=run, channel="telegram")
    [pending] = m.pending_actions(kind="run_command")
    assert "Waiting on your yes in Telegram" in reply and commands == []
    assert m.db.execute("SELECT COUNT(*) FROM parked_turns WHERE consumed = 0").fetchone()[0] == 1
    sent = []
    assert loop.check_parked(m, loop.sender_for(SimpleNamespace(send=sent.append))) == 0  # still waiting
    m.resolve_action(pending["id"], "done")  # he tapped Run; it ran
    loop.save_outcome(m, pending["id"], "[exit 0]\nremoved old.log")
    later = scripted({"reply": "Done, sir: old.log is gone.", "actions": []})
    assert loop.check_parked(m, loop.sender_for(SimpleNamespace(send=sent.append)), runner=later) == 1
    assert sent == ["Done, sir: old.log is gone."]
    assert "removed old.log" in later.prompts[0] and "delete old.log" in later.prompts[0]


def test_a_task_waits_then_the_turn_resumes_on_the_helpers_report(monkeypatch, no_claude_judge):
    m = Memory(":memory:")
    run = scripted({"reply": "On it, sir.", "actions": [{"type": "start_task", "goal": "get the tests passing"}]})
    reply = chat.respond(m, "get the tests passing", NOW, "", runner=run, channel="telegram")
    [task] = tasks.recent(m, 5)
    assert "A helper is on it" in reply
    tasks._save(m, task["id"], status="done", summary="fixed")
    reports.add(m, "task", task["id"], "done", "get the tests passing", "All 12 tests pass now.")
    sent = []
    later = scripted({"reply": "The tests pass now, sir.", "actions": []})
    loop.check_parked(m, loop.sender_for(SimpleNamespace(send=sent.append)), runner=later)
    assert sent == ["The tests pass now, sir."] and "All 12 tests pass now." in later.prompts[0]
    # this reply covered the helper's report, so it isn't passed on a second time
    assert reports.deliver(m, sent.append, compose=lambda items: "again", speaking=lambda: False) == 0


@pytest.mark.parametrize("actions", [
    [{"type": "run_command", "command": f"bad {i}"} for i in range(4)],                       # refused
    [{"type": "inspect", "op": "read", "arg": "README.md"}] * 4,                                # repeated
])
def test_three_steps_with_no_new_ok_result_stop_the_turn(monkeypatch, commands, no_claude_judge, actions):
    from jarvis import introspect
    monkeypatch.setattr(introspect, "inspect", lambda op, arg: "# JARVIS")
    use_jev(monkeypatch, ("continue", 0.99))
    m = Memory(":memory:")
    run = scripted({"reply": "Working on it.", "actions": actions}, {"reply": "Couldn't get further, sir.", "actions": []})
    chat.respond(m, "check it", NOW, "", runner=run, channel="telegram")
    assert any(s.startswith("no progress") for s in events(m, "loop_stop"))
    steps = [json.loads(s) for s in events(m, "loop_step")]
    assert len(steps) <= 4 and sum(s["status"] != "ok" for s in steps) == 3


def test_jev_failing_falls_back_to_claude(monkeypatch):
    m = Memory(":memory:")
    from jarvis import introspect
    monkeypatch.setattr(introspect, "inspect", lambda op, arg: "# JARVIS")

    def broken(state, questions):
        raise judge.JevError("Jev 529")
    monkeypatch.setattr(judge, "fast_engine", lambda: broken)
    asked = []
    monkeypatch.setattr(judge, "engine", lambda *a, **k: jev(("respond", 0.9), calls=asked))
    run = scripted({"reply": "Checking.", "actions": [{"type": "inspect", "op": "read", "arg": "README.md"}]},
                   {"reply": "It's the README, sir.", "actions": []})
    assert chat.respond(m, "what's in the readme?", NOW, "", runner=run, channel="telegram") == "It's the README, sir."
    assert len(asked) == 1 and any("Jev 529" in e for e in events(m, "jev_fallback"))
    assert json.loads(events(m, "loop_decide")[0])["by"] == "claude"


def test_low_confidence_means_respond(monkeypatch, no_claude_judge):
    m = Memory(":memory:")
    use_jev(monkeypatch, ("continue", 0.4))
    run = scripted({"reply": "Reminder set for ten, sir.",
                    "actions": [{"type": "remind", "at": SOON, "text": "Econ, sir."}]},
                   {"reply": "should not be asked", "actions": []})
    assert chat.respond(m, "remind me at 10", NOW, "", runner=run, channel="telegram") == "Reminder set for ten, sir."
    assert len(run.prompts) == 1
    decided = json.loads(events(m, "loop_decide")[0])
    assert decided["choice"] == "respond" and decided["p"] == 0.4 and decided["by"] == "jev"


def test_the_reply_never_claims_an_action_without_an_ok_result(monkeypatch, no_claude_judge):
    m = Memory(":memory:")
    monkeypatch.setattr(config, "SIRI", False)
    monkeypatch.setattr(config, "MAIL_ACCOUNT", "")
    use_jev(monkeypatch, ("respond", 0.9))
    run = scripted({"reply": "Playing it now, sir. Drafted the email too.",
                    "actions": [{"type": "play_music", "query": "lo-fi", "kind": "playlist"},
                                {"type": "draft_email", "to": ["prof@school.edu"], "subject": "Hi", "body": "x"}]})
    reply = chat.respond(m, "play lo-fi and email my prof", NOW, "", runner=run, channel="telegram")
    assert "Playing" not in reply and "Drafted" not in reply
    assert "couldn't start the music" in reply and "couldn't draft that email" in reply
    assert m.pending_actions(kind="send_email") == []


def test_actions_left_when_the_turn_stops_are_never_claimed(monkeypatch, no_claude_judge):
    m = Memory(":memory:")
    monkeypatch.setattr(config, "LOOP_MAX_STEPS", 1)
    run = scripted({"reply": "Reminder set, sir; I've texted Dana too.",
                    "actions": [{"type": "remind", "at": SOON, "text": "Econ, sir."},
                                {"type": "send_text", "to": "Dana", "body": "running late"}]})
    reply = chat.respond(m, "remind me at 10 and text Dana", NOW, "", runner=run, channel="telegram")
    assert "texted" not in reply and "couldn't set up that text" in reply
    assert m.pending_actions(kind="send_text") == [] and any("step limit" in s for s in events(m, "loop_stop"))


def test_a_reproposed_email_makes_one_draft(monkeypatch, no_claude_judge):
    m = Memory(":memory:")
    monkeypatch.setattr(config, "MAIL_ACCOUNT", "me@school.edu")
    use_jev(monkeypatch, ("continue", 0.9), ("continue", 0.9), ("respond", 0.9))
    first = {"type": "draft_email", "to": ["prof@school.edu"], "subject": "Extension", "body": "Could I have two days?"}
    better = first | {"body": "Could I please have two more days?"}
    run = scripted({"reply": "Drafted.", "actions": [first]}, {"reply": "Drafted.", "actions": [first]},
                   {"reply": "It's waiting for your OK, sir.", "actions": [better]})
    chat.respond(m, "email my prof for an extension", NOW, "", runner=run, channel="telegram")
    [draft] = m.pending_actions(kind="send_email")
    assert json.loads(draft["payload"])["body"] == "Could I please have two more days?"
    assert len(m.pending_actions(status="rejected", kind="send_email")) == 1  # the earlier one, replaced
    assert any(json.loads(s)["status"] == "refused" for s in events(m, "loop_step"))  # the exact repeat


def test_a_parked_turn_resumes_once(monkeypatch, commands, no_claude_judge):
    m = Memory(":memory:")
    run = scripted({"reply": "Needs your OK, sir.", "actions": [{"type": "run_command", "command": "rm old.log"}]})
    chat.respond(m, "delete old.log", NOW, "", runner=run, channel="mic")
    [pending] = m.pending_actions(kind="run_command")
    m.resolve_action(pending["id"], "rejected")
    sent, later = [], scripted({"reply": "Left it alone, sir.", "actions": []})
    tg = SimpleNamespace(send=sent.append)
    [parked] = m.db.execute("SELECT id FROM parked_turns").fetchall()
    assert loop.resume_turn(m, parked["id"], tg.send, runner=later) is not None
    assert loop.resume_turn(m, parked["id"], tg.send, runner=later) is None
    assert loop.check_parked(m, loop.sender_for(tg), runner=later) == 0
    assert len(sent) == 1 and len(later.prompts) == 1  # the desk mic is gone: it went to Telegram, once
    assert "Sorry, sir. I couldn't run that command" in sent[0]  # he said no: never claimed as run


def test_a_wait_times_out_and_says_whats_pending(monkeypatch, commands, no_claude_judge):
    m = Memory(":memory:")
    run = scripted({"reply": "Needs your OK, sir.", "actions": [{"type": "run_command", "command": "rm old.log"}]})
    chat.respond(m, "delete old.log", NOW, "", runner=run, channel="telegram")
    sent = []
    late = datetime.now(timezone.utc) + timedelta(minutes=config.COMMAND_APPROVAL_MINUTES + 1)
    assert loop.check_parked(m, loop.sender_for(SimpleNamespace(send=sent.append)), now=late) == 1
    assert len(sent) == 1 and "stopped waiting on his yes to run rm old.log" in sent[0]
    assert loop.check_parked(m, loop.sender_for(SimpleNamespace(send=sent.append)), now=late) == 0


def test_only_code_written_progress_shows_mid_turn(monkeypatch, screen, no_claude_judge):
    m = Memory(":memory:")
    use_jev(monkeypatch, ("continue", 0.9))
    drafts, streamed = [], []
    run = scripted({"reply": "It's definitely fine, sir.", "actions": [{"type": "look_at_screen"}]},
                   {"reply": "There's a KeyError, sir.", "actions": []})
    vision = lambda system, prompt, image: "A KeyError."  # noqa: E731
    chat.respond(m, "what's this?", NOW, "", runner=run, vision=vision, on_text=streamed.append,
                 on_draft=drafts.append, channel="mac")
    assert streamed == [] and set(drafts) <= set(loop.PROGRESS.values()) and "Having a look…" in drafts


# ----- AGENT_LOOP off -----
def test_agent_loop_off_is_the_one_pass_respond(monkeypatch, screen):
    monkeypatch.setattr(config, "AGENT_LOOP", False)
    monkeypatch.setattr(loop, "respond", lambda *a, **k: pytest.fail("the loop ran"))
    m = Memory(":memory:")
    run = scripted({"reply": "Having a look, sir.", "actions": [{"type": "look_at_screen", "question": "error"}]})
    vision = lambda system, prompt, image: "A KeyError in chat.py, line 40, sir."  # noqa: E731
    assert chat.respond(m, "what's this error?", NOW, "", runner=run, vision=vision) == "A KeyError in chat.py, line 40, sir."
    assert len(run.prompts) == 1 and not events(m, "loop_step")  # the look's answer is the reply, as before
    assert m.db.execute("SELECT COUNT(*) FROM parked_turns").fetchone()[0] == 0
