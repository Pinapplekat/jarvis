"""The terminal and projects apps end to end: the host's action path, a real WebSocket, a real node runtime. Nothing
here starts Claude: tasks run against a fake `query`."""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from claude_agent_sdk import ResultMessage
from node_harness import NodeThread, pair, start_hub, wait_for

from jarvis.apps.terminal import agent
from jarvis.contract.loader import DEFAULT_APPS_DIR
from jarvis.core import actions
from jarvis.core.apps import load_apps

CLI = actions.Origin("cli")
MAC = pytest.mark.skipif(sys.platform != "darwin", reason="needs macOS sandbox-exec")


def git(folder, *args):
    subprocess.run(["git", "-C", str(folder), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True,
                   capture_output=True, stdin=subprocess.DEVNULL)


@pytest.fixture
def link(data_dir, tmp_path, monkeypatch):
    work = (tmp_path / "work").resolve()
    project = work / "proj"
    project.mkdir(parents=True)
    git(project, "init", "-q")
    (project / "README.md").write_text("hi\n")
    git(project, "add", "README.md")
    git(project, "commit", "-q", "-m", "first")
    data_dir.mkdir(parents=True)
    (data_dir / "settings.toml").write_text(
        f'[apps.terminal]\nwork_roots = "{work}"\nread_only = "{tmp_path / "ro"}"\n')
    from jarvis.core.settings import load_settings

    played = []

    def fake_query(prompt, options):
        async def play():
            played.append(prompt)
            yield ResultMessage("success", 1, 1, False, 1, "sess-1", result="STATUS: done\nAdded hello.py; it passes.")
        return play()

    monkeypatch.setattr(agent, "query", fake_query)
    monkeypatch.setattr(agent, "wrapper", lambda *a, **k: Path("/nonexistent"))
    reg = load_apps(load_settings())
    hub = start_hub(reg)
    node = NodeThread(tmp_path, hub.port, pair(reg), apps_dir=DEFAULT_APPS_DIR).start()
    node.wait_connected()
    wait_for(hub.connected, what="the hub to see the node")
    yield SimpleNamespace(reg=reg, project=project, played=played, node=node)
    node.runtime.close()
    node.stop()
    hub.stop()
    reg.close()


def outbox(reg):
    return [r["text"] for r in reg.conn.execute("SELECT text FROM outbox ORDER BY id")]


@MAC
def test_run_goes_to_the_node_and_back_without_asking(link):
    out = actions.run(link.reg, "terminal__run", {"command": "git status --short --branch", "cwd": "proj"}, CLI)
    assert isinstance(out, actions.Ran) and out.result.text.startswith("[exit 0]")
    assert actions.recent_log(link.reg)[-1]["tool"] == "terminal__run"


def test_a_command_that_needs_a_yes_asks_with_the_exact_command_only_through_run_with_ok(link):
    args = {"command": "git push", "cwd": str(link.project), "why": "push the README fix"}
    ask = actions.run(link.reg, "terminal__run_with_ok", args, CLI, allowed={"terminal__run_with_ok"})
    assert isinstance(ask, actions.Asked)
    assert ask.question == f"Run `git push` in {link.project}? It's to push the README fix."
    done = actions.answer_ask(link.reg, ask.ask_id, True)
    assert isinstance(done, actions.Ran) and not done.result.text.startswith("[exit 0]")  # no remote: fails plainly


def test_deleting_a_file_moves_it_to_the_trash_and_undo_brings_it_back(link, tmp_path, monkeypatch):
    from jarvis.apps.terminal import run as terminal_run

    monkeypatch.setattr(terminal_run, "TRASH_DIR", tmp_path / "Trash")  # the node runs in this process
    (link.project / "notes.txt").write_text("x")
    out = actions.run(link.reg, "terminal__run", {"command": "rm notes.txt", "cwd": "proj"}, CLI)
    assert isinstance(out, actions.Ran) and not (link.project / "notes.txt").exists()
    back = actions.undo(link.reg)
    assert isinstance(back, actions.Ran) and (link.project / "notes.txt").read_text() == "x"


def test_a_task_asks_with_the_plan_every_time_runs_and_one_message_comes_back_from_the_node(link):
    args = {"goal": "add hello.py", "folder": "proj", "plan": "write hello.py and a test, then commit"}
    ask = actions.run(link.reg, "terminal__start_task", args, CLI, allowed={"terminal__start_task"})
    assert isinstance(ask, actions.Asked) and ask.question == (
        "Start a task in proj: write hello.py and a test, then commit. Go?")
    assert isinstance(actions.run(link.reg, "terminal__start_task", args, CLI, allowed={"terminal__start_task"}),
                      actions.Asked)  # a learned rule doesn't help
    assert link.played == []
    assert isinstance(actions.answer_ask(link.reg, ask.ask_id, True), actions.Ran)
    wait_for(lambda: outbox(link.reg), 10, "the task's message to reach the host")
    assert outbox(link.reg) == ["✅ add hello.py: Added hello.py; it passes."]
    assert "write hello.py and a test, then commit" in link.played[0]
    status = actions.run(link.reg, "terminal__task_status", {}, CLI)
    assert "done" in status.result.text and status.result.data["status"] == "done"
    wait_for(lambda: not link.node.runtime.unacked(), 10, "the ack")


def test_projects_shows_live_git_state_through_the_connected_node(link):
    added = actions.run(link.reg, "projects__add_project",
                        {"name": "Proj", "path": str(link.project), "kind": "personal"}, CLI)
    assert isinstance(added, actions.Ran)
    out = actions.run(link.reg, "projects__show_project", {"name": "proj"}, CLI)
    assert isinstance(out, actions.Ran) and "Git: proj: on " in out.result.text and '"first"' in out.result.text
    sync = link.reg.apps["projects"]
    from jarvis.contract.api import JobRun
    from datetime import datetime, timezone

    result = sync.module.sync(sync.ctx, JobRun(datetime.now(timezone.utc), None, "manual", False))
    assert result.summary == "1 repos, 0 new"
