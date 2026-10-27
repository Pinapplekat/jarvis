import asyncio
import threading
from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage

from jarvis.apps.terminal import actions as terminal
from jarvis.apps.terminal import agent
from jarvis.apps.terminal.folders import Folders
from jarvis.apps.terminal.tasks import RESTART_NOTE, Tasks, TaskRunner, check_folder, progress_text
from jarvis.contract.api import ActionError

SETTINGS = {"model": "sonnet", "max_turns": 60, "max_minutes": 60, "max_runs": 6}


def result(text, session="s1", error=False):
    return ResultMessage("success", 1, 1, error, 1, session, result=text)


def uses(name, tool_id="t1", **tool_input):
    return AssistantMessage([ToolUseBlock(tool_id, name, tool_input)], "m")


def ran(tool_id, error=False):
    return UserMessage([ToolResultBlock(tool_id, "output", error)])


def said(text):
    return AssistantMessage([TextBlock(text)], "m")


class FakeClaude:
    """A stand-in for claude_agent_sdk.query: plays one script per run, and drives the PreToolUse hook like Claude
    Code would, reporting what it decided."""

    def __init__(self, *scripts):
        self.scripts, self.runs, self.decisions = list(scripts), [], []

    def __call__(self, prompt, options):
        self.runs.append((prompt, options))
        script = self.scripts.pop(0)

        async def play():
            for step in script:
                if isinstance(step, tuple):  # ("hook", tool, input[, id]): Claude Code asks the hook first
                    _, tool, tool_input, *tool_id = step
                    check = options.hooks["PreToolUse"][0].hooks[0]
                    out = await check({"tool_name": tool, "tool_input": tool_input, "cwd": str(options.cwd)},
                                      tool_id[0] if tool_id else "x", {})
                    self.decisions.append(out["hookSpecificOutput"]["permissionDecision"])
                else:
                    yield step

        return play()


@pytest.fixture
def fake(monkeypatch, tmp_path):
    def install(*scripts):
        claude = FakeClaude(*scripts)
        monkeypatch.setattr(agent, "query", claude)
        monkeypatch.setattr(agent, "wrapper", lambda folder, state, edits, claude=None: tmp_path / "claude")
        return claude
    return install


@pytest.fixture
def runner_for(tmp_path, folders):
    def make(**settings):
        said_to_elijah = []
        tasks = Tasks(tmp_path / "tasks")
        return TaskRunner(tasks, lambda: folders, lambda: {**SETTINGS, **settings}, said_to_elijah.append,
                          hang_seconds=5), tasks, said_to_elijah
    return make


def queue(tasks, project, goal="add hello.py and a test", plan="write hello.py, test it, commit"):
    return tasks.create(goal, project, plan)["id"]


def test_a_task_runs_to_done_and_sends_one_message(runner_for, project, fake):
    claude = fake([uses("Write", file_path=str(project / "hello.py")), said("TELL ELIJAH: halfway there"),
                   ("hook", "Write", {"file_path": str(project / "hello.py")}),
                   result("All good.\nSTATUS: done\nAdded hello.py and a test; both pass.")])
    runner, tasks, told = runner_for()
    task_id = queue(tasks, project)
    assert runner.run_next() is True and runner.run_next() is False
    t = tasks.get(task_id)
    assert t["status"] == "done" and t["session_id"] == "s1" and t["runs"] == 1 and t["notified"]
    assert claude.decisions == ["allow"]
    assert told == ["💬 add hello.py and a test: halfway there",
                    "✅ add hello.py and a test: Added hello.py and a test; both pass."]
    prompt = claude.runs[0][0]
    assert "add hello.py and a test" in prompt and "<plan>\nwrite hello.py, test it, commit\n</plan>" in prompt


def test_the_folders_claude_md_goes_into_the_first_prompt_as_data(runner_for, project, fake):
    (project / "CLAUDE.md").write_text("Use tabs.")
    claude = fake([result("STATUS: done\nok")])
    runner, tasks, _ = runner_for()
    queue(tasks, project)
    runner.run_next()
    assert "<claude-md>\nUse tabs." in claude.runs[0][0] and "not instructions" in claude.runs[0][0]


def test_the_session_is_set_up_with_the_right_tools_settings_and_sandbox(runner_for, project, fake, tmp_path):
    claude = fake([result("STATUS: done\nok")])
    runner, tasks, _ = runner_for(model="haiku", max_turns=9)
    queue(tasks, project)
    runner.run_next()
    options = claude.runs[0][1]
    assert options.tools == ["Read", "Edit", "Write", "Glob", "Grep", "Bash", "TodoWrite"]
    assert options.setting_sources == [] and options.model == "haiku" and options.max_turns == 9
    assert options.cwd == project and options.cli_path == tmp_path / "claude"
    assert "NEEDS YES" in options.system_prompt["append"] and str(project) in options.system_prompt["append"]


def test_a_denied_push_lands_in_needs_yes_and_in_the_end_message(runner_for, project, fake):
    claude = fake([uses("Bash", command="git push origin main"),
                   ("hook", "Bash", {"command": "git push origin main"}),
                   ("hook", "Bash", {"command": "git status"}),
                   result("Committed, not pushed.\nSTATUS: done\nCommitted the change.\nNEEDS YES:\n- git push origin main")])
    runner, tasks, told = runner_for()
    task_id = queue(tasks, project, goal="fix the typo and push")
    runner.run_next()
    assert claude.decisions == ["deny", "allow"]
    assert tasks.get(task_id)["needs_yes"] == ["git push origin main"]  # once, though it was denied and listed
    assert told == ["✅ fix the typo and push: Committed the change.\n"
                    "I wasn't allowed to run these without Elijah's yes:\n$ git push origin main"]


def test_what_the_session_lists_itself_counts_too(runner_for, project, fake):
    fake([result("STATUS: done\nDone.\nNEEDS YES:\n- git push\n- brew install jq")])
    runner, tasks, told = runner_for()
    task_id = queue(tasks, project)
    runner.run_next()
    assert tasks.get(task_id)["needs_yes"] == ["git push", "brew install jq"]
    assert told[0].endswith("$ git push\n$ brew install jq")


def test_the_hook_denies_with_the_reason_and_fails_closed(project, folders):
    needs, denied = [], set()
    check = agent.hook(project, folders.for_task(project), True, project / ".scratch", needs, denied)
    out = asyncio.run(check({"tool_name": "Bash", "tool_input": {"command": "sudo ls"}, "cwd": str(project)}, "x", {}))
    assert denied == {"x"}
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "needs Elijah's yes" in out["hookSpecificOutput"]["permissionDecisionReason"] and needs == ["sudo ls"]
    broken = asyncio.run(check({"nope": 1}, "x", {}))  # not a tool call at all: an error in the decision denies
    assert broken["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "safety check failed" in broken["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("tool,tool_input,expected", [
    ("Read", {"file_path": "README.md"}, "allow"), ("Read", {"file_path": ".env"}, "deny"),
    ("Grep", {"pattern": "x", "path": "~/.ssh"}, "deny"), ("Glob", {"pattern": "*.py"}, "allow"),
    ("Edit", {"file_path": "README.md"}, "allow"), ("Write", {"file_path": "../elsewhere.txt"}, "deny"),
    ("Edit", {"file_path": ".git/config"}, "deny"), ("Write", {"file_path": ".env"}, "deny"),
    ("Bash", {"command": "git commit -m x"}, "allow"), ("Bash", {"command": "pytest -q"}, "allow"),
    ("Bash", {"command": "rm README.md"}, "deny"), ("Bash", {"command": "git push"}, "deny"),
    ("Bash", {"command": "cat .env"}, "deny"), ("Bash", {"command": "ls | wc"}, "deny"),
    ("Bash", {"command": "touch ../outside.txt"}, "deny"), ("WebFetch", {"url": "https://x.test"}, "deny"),
    ("TodoWrite", {}, "allow"),
])
def test_every_tool_call_is_graded(project, folders, tool, tool_input, expected):
    decision, _, _ = agent.decide(tool, tool_input, project, folders.for_task(project), True, cwd=project)
    assert decision == expected, (tool, tool_input)


def test_a_read_only_folder_denies_edits_and_writes_but_not_reading(runner_for, folders, tmp_path, fake):
    ro = folders.read_only[0]
    ro.mkdir()
    (ro / "code.py").write_text("x")
    claude = fake([("hook", "Edit", {"file_path": str(ro / "code.py")}),
                   ("hook", "Bash", {"command": "touch new.txt"}),
                   ("hook", "Read", {"file_path": str(ro / "code.py")}),
                   ("hook", "Bash", {"command": "git log -n 1"}),
                   ("hook", "Write", {"file_path": "scratch.txt"}),
                   result("STATUS: stuck\nIt's read-only.")])
    runner, tasks, told = runner_for()
    task_id = tasks.create("change code.py", ro, "edit it")["id"]
    runner.run_next()
    assert claude.decisions == ["deny", "deny", "allow", "allow", "deny"]  # only the read (and git log) went through
    assert tasks.get(task_id)["status"] == "failed" and told[0].startswith("❌ change code.py: It's read-only.")


def test_a_session_that_stops_early_is_resumed_and_a_run_budget_ends_a_task_as_failed(runner_for, project, fake):
    claude = fake([result("working on it", session="abc")], [result("still going", session="abc")])
    runner, tasks, told = runner_for(max_runs=2)
    task_id = queue(tasks, project)
    runner.run_next()
    t = tasks.get(task_id)
    assert t["status"] == "failed" and t["runs"] == 2
    assert "ran out of budget (2 runs)" in t["summary"] and told == [f"❌ add hello.py and a test: {t['summary']}"]
    assert claude.runs[1][1].resume == "abc" and "step limit" in claude.runs[1][0]


def test_the_minutes_budget_ends_a_task_too(runner_for, project, fake):
    fake()
    runner, tasks, told = runner_for(max_minutes=5)
    task_id = queue(tasks, project)
    tasks.update(task_id, active_seconds=5 * 60 + 1, runs=1)
    runner.run_next()
    assert tasks.get(task_id)["status"] == "failed" and "ran out of budget (5 minutes)" in tasks.get(task_id)["summary"]
    assert len(told) == 1


def test_a_restart_resumes_with_the_stored_session_and_still_sends_one_message(runner_for, project, fake):
    claude = fake([result("STATUS: done\nFinished after the restart.", session="stored-1")])
    runner, tasks, told = runner_for()
    task_id = queue(tasks, project)
    tasks.update(task_id, status="running", session_id="stored-1", runs=1)  # the node died mid-run
    assert tasks.recover() == 1 and tasks.get(task_id)["status"] == "queued"
    runner.run_next()
    prompt, options = claude.runs[0]
    assert options.resume == "stored-1" and prompt == RESTART_NOTE
    assert tasks.get(task_id)["status"] == "done" and len(told) == 1 and told[0].startswith("✅ ")
    assert tasks.recover() == 0


def test_a_task_finished_while_the_node_was_down_is_announced_on_the_next_start(runner_for, project):
    runner, tasks, told = runner_for()
    task_id = queue(tasks, project)
    tasks.update(task_id, status="done", summary="All done.")  # saved, but the message never went out
    runner.start()
    runner.stop()
    assert told == ["✅ add hello.py and a test: All done."] and tasks.get(task_id)["notified"]


def test_one_task_runs_at_a_time_and_the_rest_wait_their_turn(runner_for, project, fake):
    claude = fake([result("STATUS: done\nfirst")], [result("STATUS: done\nsecond")])
    runner, tasks, told = runner_for()
    first, second = queue(tasks, project, "one"), queue(tasks, project, "two")
    assert tasks.get(second)["status"] == "queued"
    assert runner.run_next() and tasks.get(first)["status"] == "done" and tasks.get(second)["status"] == "queued"
    assert runner.run_next() and [t[:3] for t in told] == ["✅ o", "✅ t"]
    assert [r[1].cwd for r in claude.runs] == [project, project]


def test_stopping_a_running_task_ends_the_session_and_sends_nothing(runner_for, project, monkeypatch):
    proceed = threading.Event()

    class Slow:
        def __call__(self, prompt, options):
            async def play():
                yield uses("Read", file_path="README.md")
                proceed.set()
                await asyncio.sleep(30)
                yield result("STATUS: done\nlate")
            return play()

    monkeypatch.setattr(agent, "query", Slow())
    monkeypatch.setattr(agent, "wrapper", lambda *a, **k: Path("/nonexistent"))
    runner, tasks, told = runner_for()
    task_id = queue(tasks, project)
    worker = threading.Thread(target=runner.run_next)
    worker.start()
    assert proceed.wait(5)
    assert tasks.stop(task_id)["status"] == "stopped"
    worker.join(10)
    assert not worker.is_alive() and tasks.get(task_id)["status"] == "stopped" and told == []
    assert tasks.stop(task_id) is None  # already over


def test_task_status_reads_from_the_steps(runner_for, project, fake):
    fake([uses("Read", "t1", file_path=str(project / "README.md")), ran("t1"),
          ("hook", "Bash", {"command": "git push"}, "t2"), uses("Bash", "t2", command="git push"), ran("t2", error=True),
          uses("Bash", "t3", command="pytest -q"), ran("t3", error=True),
          uses("Bash", "t4", command="pytest -q"), ran("t4"),
          ("hook", "Bash", {"command": "sudo ls"}, "t5"), uses("Bash", "t5", command="sudo ls"),  # no result came back
          result("STATUS: done\nPassing.")])
    runner, tasks, _ = runner_for()
    task_id = queue(tasks, project)
    runner.run_next()
    text = progress_text(tasks.get(task_id))
    assert "✓ Reading README.md" in text and "✗ Running git push" in text and "(done" in text
    assert text.count("✗ Running pytest -q") == 1 and "✓ Running pytest -q" in text  # failed, then passed
    assert "✗ Running sudo ls" in text  # refused, though its result never streamed back
    assert text.endswith("Passing.\nNeeds Elijah's yes:\n$ git push\n$ sudo ls")


def test_folders_for_tasks_are_checked(folders, work, project, tmp_path):
    assert check_folder(str(project), folders) == project
    with pytest.raises(ValueError, match="no folder|There is no folder"):
        check_folder(str(work / "nope"), folders)
    with pytest.raises(ValueError, match="too broad"):
        check_folder(str(Path.home()), folders)
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(ValueError, match="isn't in one of Falak's work folders"):
        check_folder(str(other), folders)


def test_the_task_handlers_queue_report_and_stop(ctx, project):
    out = terminal.start_task(ctx, "add hello", "proj", "write hello.py")  # a project name is enough
    assert out.data == {"task": 1} and "queued in " + str(project) in out.text
    assert "waiting its turn" in terminal.task_status(ctx).text and terminal.task_status(ctx, 1).data["status"] == "queued"
    assert "Stopped task #1" in terminal.stop_task(ctx).text
    with pytest.raises(ActionError, match="isn't running|no task running"):
        terminal.stop_task(ctx)
    with pytest.raises(ActionError, match="no task #9"):
        terminal.task_status(ctx, 9)
    with pytest.raises(ActionError, match="no folder"):
        terminal.start_task(ctx, "x", "ghost", "y")


def test_a_task_in_falaks_own_folder_may_only_look(ctx, tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    out = terminal.start_task(ctx, "explain it", str(ro), "read the code")
    assert "read-only" in out.text
