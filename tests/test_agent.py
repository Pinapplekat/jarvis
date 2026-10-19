import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from jarvis import agent, config

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS sandbox")


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "site"
    (r / "src").mkdir(parents=True)
    (r / "src" / "app.py").write_text("x = 1\n")
    (r / ".env").write_text("KEY=secret\n")
    monkeypatch.setattr(config, "PROJECT_DIR", tmp_path / "jarvis-proj")
    return r


@pytest.mark.parametrize("tool,tool_input,decision", [
    ("Read", {"file_path": "src/app.py"}, "allow"),
    ("Read", {"file_path": "/etc/hosts"}, "allow"),                       # reads anywhere...
    ("Read", {"file_path": ".env"}, "deny"),                              # ...but secrets
    ("Read", {"file_path": str(Path.home() / ".ssh/id_ed25519")}, "deny"),
    ("Grep", {"pattern": "x", "path": str(Path.home() / "Library/Keychains")}, "deny"),
    ("Glob", {"pattern": "**/*.py"}, "allow"),
    ("Edit", {"file_path": "src/app.py", "old_string": "1", "new_string": "2"}, "allow"),
    ("Write", {"file_path": "/tmp/elsewhere.py", "content": ""}, "deny"),  # edits stay inside root
    ("Write", {"file_path": ".git/config", "content": ""}, "deny"),
    ("Edit", {"file_path": ".env"}, "deny"),
    ("Bash", {"command": "pytest -q"}, "allow"),
    ("Bash", {"command": "git commit -m fix"}, "allow"),                   # medium: inside root
    ("Bash", {"command": "cat /etc/hosts"}, "allow"),
    ("Bash", {"command": "git push"}, "ask"),                              # high: Elijah's call
    ("Bash", {"command": "sudo rm -rf /"}, "ask"),
    ("Bash", {"command": "frobnicate --all"}, "ask"),                      # unknown is high
    ("Bash", {"command": "cd src && pytest"}, "deny"),                     # shell syntax: one plain command
    ("Bash", {"command": "cat .env"}, "deny"),
    ("WebFetch", {"url": "https://docs.python.org/3/"}, "allow"),
    ("WebFetch", {"url": "https://www.chase.com/"}, "deny"),
    ("TodoWrite", {"todos": []}, "allow"),
    ("Task", {"prompt": "do it all"}, "deny"),                             # no subagents
    ("mcp__x__y", {}, "deny"),
])
def test_decide(root, tool, tool_input, decision):
    assert agent.decide(tool, tool_input, root)[0] == decision


def test_read_only_folder_allows_reading_but_nothing_that_changes_it(root):
    assert agent.decide("Read", {"file_path": "src/app.py"}, root, edits=False)[0] == "allow"
    assert agent.decide("Edit", {"file_path": "src/app.py"}, root, edits=False)[0] == "deny"
    assert agent.decide("Bash", {"command": "touch x"}, root, edits=False)[0] == "deny"
    assert agent.decide("Bash", {"command": "python -m pytest -q"}, root, edits=False)[0] == "allow"


def event(tool, **tool_input):
    return json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input, "cwd": ""})


def test_hook_pauses_a_risky_step_and_records_it(root, tmp_path):
    state = tmp_path / "state"
    out = agent.run_hook(event("Bash", command="git push origin main", description="publish"), root, state)
    assert out["continue"] is False and out["hookSpecificOutput"]["permissionDecision"] == "deny"
    blocked = json.loads((state / agent.BLOCKED).read_text())
    assert blocked["command"] == "git push origin main" and blocked["why"] == "publish" and blocked["risk"] == "high"


def test_hook_allows_and_denies_without_stopping(root, tmp_path):
    ok = agent.run_hook(event("Read", file_path="src/app.py"), root, tmp_path)
    no = agent.run_hook(event("Read", file_path=".env"), root, tmp_path)
    assert ok["hookSpecificOutput"]["permissionDecision"] == "allow" and "continue" not in ok
    assert no["hookSpecificOutput"]["permissionDecision"] == "deny" and "continue" not in no


def test_hook_fails_closed(root, tmp_path):
    assert agent.run_hook("not json", root, tmp_path)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_the_real_hook_script_runs_isolated_and_answers(root, tmp_path):
    env = {**agent.environment(root, tmp_path), "PATH": os.environ["PATH"]}
    r = subprocess.run([sys.executable, "-I", str(agent.HOOK_SCRIPT)], input=event("Bash", command="git push"),
                       capture_output=True, text=True, env=env, timeout=30)
    assert json.loads(r.stdout)["continue"] is False
    r = subprocess.run([sys.executable, "-I", str(agent.HOOK_SCRIPT)], input=event("Bash", command="ls"),
                       capture_output=True, text=True, env={"PATH": os.environ["PATH"]}, timeout=30)
    assert json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"  # no root given: closed


def test_claude_sandbox_confines_writes_and_hides_secrets(root, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    profile = agent.claude_sandbox(root, state)
    outside = Path.home() / f".jarvis-agent-test-{os.getpid()}"

    def sh(code):
        return subprocess.run(["/usr/bin/sandbox-exec", "-p", profile, sys.executable, "-I", "-c", code],
                              capture_output=True, text=True, cwd=root, timeout=30)
    assert sh(f"open({str(root / 'src' / 'new.py')!r}, 'w').write('ok')").returncode == 0
    assert sh(f"open({str(state / 'blocked.json')!r}, 'w').write('{{}}')").returncode == 0
    try:
        assert sh(f"open({str(outside)!r}, 'w')").returncode != 0 and not outside.exists()
    finally:
        outside.unlink(missing_ok=True)
    assert sh("open('.env').read()").returncode != 0
    assert sh(f"open({str(Path.home() / '.claude' / 'settings.json')!r}, 'a')").returncode != 0
    assert sh(f"import os; os.makedirs({str(root / '.git' / 'hooks')!r}, exist_ok=True)").returncode != 0
    # A read-only root (JARVIS's own project) can't be written. (The test's temp folders are
    # writable as temp space, so this root lives in the home folder.)
    ro_root = Path.home() / f".jarvis-agent-ro-{os.getpid()}"
    ro_root.mkdir()
    try:
        read_only = agent.claude_sandbox(ro_root, state, edits=False)
        r = subprocess.run(["/usr/bin/sandbox-exec", "-p", read_only, sys.executable, "-I", "-c",
                            f"open({str(ro_root / 'x.py')!r}, 'w')"], capture_output=True, cwd=ro_root, timeout=30)
        assert r.returncode != 0 and not (ro_root / "x.py").exists()
    finally:
        (ro_root / "x.py").unlink(missing_ok=True)
        ro_root.rmdir()


def test_the_session_command_is_locked_down(root, tmp_path, monkeypatch):
    monkeypatch.setattr("jarvis.chat.find_claude", lambda: "/usr/local/bin/claude")
    argv = agent.command(root, tmp_path, "sonnet", "rules", resume="abc", max_turns=12)
    assert argv[:2] == ["/usr/bin/sandbox-exec", "-p"] and argv[3] == "/usr/local/bin/claude"
    for flag in ("--strict-mcp-config", "--permission-mode", "--settings", "--resume", "--max-turns"):
        assert flag in argv
    assert argv[argv.index("--setting-sources") + 1] == "" and argv[argv.index("--tools") + 1] == agent.TOOLS
    hook = json.loads(argv[argv.index("--settings") + 1])["hooks"]["PreToolUse"][0]
    assert hook["matcher"] == "*" and str(agent.HOOK_SCRIPT) in hook["hooks"][0]["command"]
    env = agent.environment(root, tmp_path)
    assert not any("TELEGRAM" in k or "TWILIO" in k for k in env)


FAKE_CLAUDE = r'''
import json, sys, time
events = json.loads(sys.argv[1])
for e in events:
    if e == "sleep":
        time.sleep(30)
        continue
    print(json.dumps(e), flush=True)
'''


def fake_spawn(events, state=None, blocked=None):
    def spawn(argv, env, cwd):
        if blocked:
            (Path(env["JARVIS_AGENT_STATE"]) / agent.BLOCKED).write_text(json.dumps(blocked))
        return subprocess.Popen([sys.executable, "-c", FAKE_CLAUDE, json.dumps(events)], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, start_new_session=True)
    return spawn


STREAM = [
    {"type": "system", "subtype": "init", "session_id": "sess-1"},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "/x/app.py"}}]}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "TELL ELIJAH: Found the bug, sir.\nFixing."}]}},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}}]}},
    {"type": "result", "result": "Fixed.\nSTATUS: done\nThe test passes now, sir.", "is_error": False, "session_id": "sess-1"},
]


def test_run_session_streams_progress_and_parses_the_end(root, tmp_path, monkeypatch):
    monkeypatch.setattr("jarvis.chat.find_claude", lambda: "claude")
    tools, tells = [], []
    out = agent.run_session("go", root, tmp_path / "state", "sonnet", "r", on_tool=tools.append, on_tell=tells.append,
                            spawn=fake_spawn(STREAM), log_to=tmp_path / "log.jsonl")
    assert tools == ["Reading app.py", "Running pytest -q"] and tells == ["Found the bug, sir."]
    assert out.session_id == "sess-1" and out.status == "done" and out.summary == "The test passes now, sir."
    assert out.blocked is None and (tmp_path / "log.jsonl").read_text().count("\n") == len(STREAM)


def test_run_session_picks_up_a_paused_step(root, tmp_path, monkeypatch):
    monkeypatch.setattr("jarvis.chat.find_claude", lambda: "claude")
    blocked = {"tool": "Bash", "command": "git push", "cwd": str(root), "reason": "push", "risk": "high"}
    out = agent.run_session("go", root, tmp_path / "state", "sonnet", "r", spawn=fake_spawn(STREAM[:1], blocked=blocked))
    assert out.blocked == blocked and not (tmp_path / "state" / agent.BLOCKED).exists()
    assert agent.run_blocked(out.blocked) == (["git", "push"], str(root))


def test_run_session_kills_a_hang_and_honours_stop(root, tmp_path, monkeypatch):
    monkeypatch.setattr("jarvis.chat.find_claude", lambda: "claude")
    out = agent.run_session("go", root, tmp_path / "s", "m", "r", spawn=fake_spawn(STREAM[:1] + ["sleep"]), hang_seconds=1.5)
    assert out.hung and out.session_id == "sess-1"
    out = agent.run_session("go", root, tmp_path / "s", "m", "r", spawn=fake_spawn(["sleep"]), should_stop=lambda: True)
    assert out.stopped


def test_outcome_status_and_resume_notes():
    assert agent.Outcome(text="tried\nSTATUS: stuck\nNo luck, sir.").status == "stuck"
    assert agent.Outcome(text="halfway").status == "unfinished"
    yes = agent.resume_note({"command": "git push"}, "[exit 0]\nok")
    no = agent.resume_note({"command": "git push"}, "(Elijah rejected this command; find another way)")
    assert "Elijah said yes" in yes and "untrusted" in yes and "did not approve" in no
    with pytest.raises(ValueError):
        agent.run_blocked({"tool": "Write", "command": ""})


def test_scratch_folder_is_always_writable_even_when_the_project_is_read_only(tmp_path):
    from jarvis import agent
    root, state = tmp_path / "proj", tmp_path / "state"
    root.mkdir()
    scratch = agent.scratch_dir(state)
    scratch.mkdir(parents=True)
    assert agent.decide("Write", {"file_path": str(scratch / "signin.py")}, root, edits=False, scratch=scratch)[0] == "allow"
    assert agent.decide("Write", {"file_path": "/tmp/x.py"}, root, edits=False, scratch=scratch)[0] == "deny"
    assert agent.decide("Write", {"file_path": str(root / "a.py")}, root, edits=False, scratch=scratch)[0] == "deny"
    assert agent.decide("Write", {"file_path": str(scratch / ".env")}, root, scratch=scratch)[0] == "deny"  # secrets still


def test_rules_mention_scratch_and_no_typed_input():
    from jarvis import agent
    rules = agent.AGENT_RULES.format(root="/r", scratch="/s/scratch")
    assert "/s/scratch" in rules and "Nothing can type into a command" in rules


def test_a_job_that_wanted_typed_input_says_so(tmp_path, monkeypatch):
    import time
    from jarvis import jobs
    from jarvis.memory import Memory
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path)
    (tmp_path / "1").mkdir()
    (tmp_path / "1" / "job.log").write_text("Enter code: Traceback\nEOFError: EOF when reading a line\n")
    jobs._save_meta({"id": 1, "argv": ["python3", "signin.py"], "cwd": str(tmp_path), "pid": 999999,
                     "started": time.time() - 5, "action_id": None, "why": "", "notified": {}})
    monkeypatch.setattr(jobs, "job_status", lambda job_id, lines=8, now=None: {
        "state": "exited", "code": 1, "tail": "EOFError: EOF when reading a line", "runtime": 5,
        "last_output": time.time(), "argv": ["python3", "signin.py"], "id": 1})
    monkeypatch.setattr(jobs, "describe_state", lambda s: "failed (exit 1)")
    monkeypatch.setattr(jobs, "_command", lambda s: "python3 signin.py")
    m = Memory(":memory:")
    assert jobs.check_jobs(m) == 1
    text = m.db.execute("SELECT text FROM reports").fetchone()[0]
    assert "waiting for typed input" in text
