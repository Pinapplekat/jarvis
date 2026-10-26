import json
import os
import time

from jarvis import helpers, selfedit
from jarvis.memory import Memory


def transcript(name, folder, events, age=0, project="-Users-x-site"):
    d = helpers.PROJECTS / project
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    if age:
        t = time.time() - age
        os.utime(path, (t, t))
    return path


def user(text, folder, entry="cli"):
    return {"type": "user", "cwd": folder, "entrypoint": entry, "message": {"role": "user", "content": text}}


def tool(name, folder, **inp):
    return {"type": "assistant", "cwd": folder, "entrypoint": "cli",
            "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}}


def said(text, folder):
    return {"type": "assistant", "cwd": folder, "entrypoint": "cli", "message": {"content": [{"type": "text", "text": text}]}}


def test_it_sees_what_a_session_is_doing_from_its_transcript():
    transcript("abc123", "/Users/x/site", [
        {"type": "ai-title", "aiTitle": "Picture sending"},
        user("Build plan 16: screenshots to Telegram", "/Users/x/site"),
        tool("Read", "/Users/x/site", file_path="/Users/x/site/jarvis/telegram.py"),
        tool("Bash", "/Users/x/site", command="pytest -q"),
        said("Tests pass; wiring the CLI next.", "/Users/x/site"),
    ])
    [s] = helpers.claude_sessions(Memory(":memory:"))
    assert s["title"] == "Picture sending" and s["state"] == "working now"
    assert s["steps"] == ["Reading telegram.py", "Running pytest -q"] and s["said"].startswith("Tests pass")
    text = helpers.for_prompt(Memory(":memory:"))
    assert '"Picture sending" in site: working now' in text and "screenshots to Telegram" in text
    assert "Tests pass; wiring the CLI next." in helpers.detail(Memory(":memory:"), "picture")


def test_quiet_sessions_say_so_and_old_ones_drop_off():
    transcript("quiet", "/Users/x/site", [user("fix it", "/Users/x/site")], age=600)
    transcript("old", "/Users/x/site", [user("ancient", "/Users/x/site")], age=helpers.RECENT_HOURS * 3600 + 60)
    sessions = helpers.claude_sessions(Memory(":memory:"))
    assert [s["id"] for s in sessions] == ["quiet"] and sessions[0]["state"] == "quiet for 10 min"


def test_jarvis_own_sessions_and_finished_scripts_are_left_out():
    m = Memory(":memory:")
    tid = m.db.execute("INSERT INTO tasks (goal, created_at, updated_at, session_id) VALUES ('x', 'n', 'n', 'task-sess')").lastrowid
    transcript("task-sess", "/Users/x/site", [user("a task", "/Users/x/site")])
    transcript("script", "/Users/x/site", [user("say ok", "/Users/x/site", entry="sdk-cli")], age=600)
    assert tid and helpers.claude_sessions(m) == []


def test_secrets_in_transcripts_are_masked():
    transcript("leaky", "/Users/x/site", [user("my key is sk-ant-" + "z" * 30, "/Users/x/site")])
    assert "zzzz" not in helpers.for_prompt(Memory(":memory:"))


def test_it_says_when_self_editing_is_off(monkeypatch):
    monkeypatch.setattr(selfedit, "enabled", lambda: False)
    assert "self-editing: OFF" in helpers.for_prompt(Memory(":memory:"))


def test_it_says_what_the_build_is_doing_when_self_editing_is_on(monkeypatch):
    monkeypatch.setattr(selfedit, "enabled", lambda: True)
    m = Memory(":memory:")
    eid = m.add_self_edit("request", "README note", "add it")
    m.set_self_edit(eid, status="ready")
    text = helpers.for_prompt(m)
    assert "self-editing: ON; your build session is idle" in text and "waiting for his Deploy/Discard: README note" in text


def test_the_board_follows_a_sessions_plan_and_says_where_it_runs():
    folder = "/Users/x/site"
    plan = {"type": "assistant", "cwd": folder, "entrypoint": "claude-desktop", "gitBranch": "hud-fix",
            "message": {"content": [{"type": "tool_use", "name": "mcp__ccd_turn__update_plan", "input": {"items": [
                {"content": "Read the code", "status": "in_progress"}, {"content": "Fix the bug", "status": "pending"},
                {"content": "Test", "status": "pending"}]}}]}}
    advance = {**plan, "message": {"content": [{"type": "tool_use", "name": "mcp__ccd_turn__update_plan",
                                                "input": {"advance": True}}]}}
    transcript("desk", folder, [{"type": "ai-title", "aiTitle": "HUD fix"}, user("fix the hud", folder),
                                plan, tool("Read", folder, file_path="/Users/x/site/hud.py"), advance])
    m = Memory(":memory:")
    tid = m.db.execute("INSERT INTO tasks (goal, status, steps, created_at, updated_at) VALUES "
                       "('tidy the repo', 'waiting', ?, '2026-10-19T10:00:00+00:00', '2026-10-19T10:00:00+00:00')",
                       (json.dumps([{"n": 1, "update": "Asking before deleting build/"}]),)).lastrowid
    board = helpers.board(m)
    assert [h["kind"] for h in board] == ["task", "session"]  # waiting on him first
    task, desk = board
    assert task["name"] == f"Task #{tid}: tidy the repo" and task["waiting"] and task["now"] == "Asking before deleting build/"
    assert desk["where"] == "Claude app · site · hud-fix" and desk["busy"] and desk["now"] == "Fix the bug"
    assert [x["status"] for x in desk["plan"]] == ["completed", "in_progress", "pending"]
    assert desk["steps"] == ["Reading hud.py"]  # plan bookkeeping isn't listed as a step
