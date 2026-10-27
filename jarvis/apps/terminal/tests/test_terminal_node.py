"""The terminal app inside a real NodeRuntime: its start handler runs once, its watcher's message goes out through
the runtime's outbox, and stopping the runtime stops the threads."""

import json
import sys
import time

import pytest
from conftest import MAC

from jarvis.apps.terminal import actions as terminal
from jarvis.apps.terminal import jobs
from jarvis.contract.loader import DEFAULT_APPS_DIR
from jarvis.contract.manifest import load_manifest
from jarvis.node.runtime import NodeRuntime
from jarvis.protocol import messages as m


@pytest.fixture
def runtime(tmp_path, work, monkeypatch):
    monkeypatch.setattr(jobs, "CHECK_EVERY", 0.1)
    node_dir = tmp_path / "node"
    node_dir.mkdir()
    rt = NodeRuntime(node_dir, node_dir / "tmp")
    manifest = load_manifest(DEFAULT_APPS_DIR / "terminal" / "manifest.toml")
    settings = {k: v["default"] for k, v in manifest.settings.items()} | {"work_roots": str(work)}
    rt.configure({"terminal": settings})
    yield rt
    rt.close()


def test_the_node_runs_the_apps_start_once_and_stop_when_it_closes(runtime):
    assert terminal._service is not None
    first = terminal._service
    ctx = runtime._apps["terminal"].ctx
    runtime.configure({"terminal": {**ctx.settings, "max_runs": 2}})  # the host link came back, with a new setting
    assert terminal._service is first and runtime._apps["terminal"].ctx is ctx and ctx.settings["max_runs"] == 2
    runtime.close()
    assert terminal._service is None


@MAC
def test_a_job_that_ends_reaches_the_runtimes_outbox_through_ctx_notify(runtime, project):
    (project / "quick.py").write_text("print('done')\n")
    ctx = runtime._apps["terminal"].ctx
    terminal.run(ctx, "python3 quick.py", str(project), background=True)
    end = time.time() + 15
    while time.time() < end and not runtime.unacked():
        time.sleep(0.1)
    (event,) = runtime.unacked()
    assert event.app == "terminal" and event.text.startswith("✅ Background job finished OK")
    assert json.loads(next((runtime._outbox).glob("*.json")).read_text())["app"] == "terminal"


def test_the_runtime_calls_a_terminal_action_the_way_the_host_does(runtime, project):
    call = m.Call(1, "terminal", "repo_state", {"path": str(project)}, 20)
    out = runtime.execute(call)
    assert out.ok and out.data["subject"] == "first" and out.data["branch"] in ("main", "master")
    bad = runtime.execute(m.Call(2, "terminal", "run", {"command": "cat .env", "cwd": str(project), "background": False}, 20))
    assert not bad.ok and "may hold secrets" in bad.error


def test_every_manifest_action_has_a_handler_and_the_dangerous_ones_ask():
    manifest = load_manifest(DEFAULT_APPS_DIR / "terminal" / "manifest.toml")
    effects = {a.name: a.effect for a in manifest.actions}
    assert effects["start_task"] == "plan" and effects["run_with_ok"] == "irreversible" and effects["run"] == "local"
    assert effects["jobs"] == effects["job_log"] == effects["find_repos"] == effects["repo_state"] == "read"
    assert all(a.timeout_s >= 130 for a in manifest.actions if a.name in ("run", "run_with_ok"))
    assert [a.name for a in manifest.actions if not a.tool] == ["find_repos"]
    assert {a.name for a in manifest.actions if a.untrusted_output} >= {"run", "job_log", "task_status"}
    assert sys.modules["jarvis.apps.terminal.actions"] is terminal
