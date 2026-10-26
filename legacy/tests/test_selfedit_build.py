"""The build session's control flow, with a fake Claude Code runner (nothing is built or deployed)."""

import pytest

from jarvis import agent, selfedit, terminal
from jarvis.memory import Memory


@pytest.fixture
def build(tmp_path, monkeypatch):
    monkeypatch.setattr(selfedit, "BUILD_DIR", tmp_path / "build")
    monkeypatch.setattr(selfedit, "STATUS", tmp_path / "build" / "status.json")
    monkeypatch.setattr(selfedit, "CLONE", tmp_path / "src")
    monkeypatch.setattr(selfedit, "LOG", tmp_path / "selfedit.log")
    monkeypatch.setattr(terminal, "AUDIT", tmp_path / "commands.jsonl")
    (tmp_path / "src").mkdir()
    return tmp_path


class FakeTG:
    def __init__(self):
        self.sent, self.edits = [], []

    def send(self, text, buttons=None):
        self.sent.append((text, buttons))
        return len(self.sent)

    def edit(self, mid, text):
        self.edits.append(text)


def runner(*outcomes):
    calls = []

    def run(prompt, root, state, model, rules, resume=None, on_tool=None, **kw):
        calls.append({"prompt": prompt, "resume": resume, "root": root})
        on_tool("Editing chat.py")
        return outcomes[min(len(calls), len(outcomes)) - 1]
    run.calls = calls
    return run


def test_progress_is_reported_to_jarvis_not_texted(build):
    from jarvis import reports
    m, tg = Memory(":memory:"), FakeTG()
    out = selfedit.run_agent(m, tg, "Status command", "build it", edit_id=4,
                             runner=runner(agent.Outcome(session_id="s", text="STATUS: done\nAdded it.")))
    assert out.summary == "Added it." and tg.sent == []
    [filed] = reports.recent(m)
    assert (filed["source"], filed["kind"], filed["ref"]) == ("build", "progress", 4) and "Editing chat.py" in filed["text"]
    assert selfedit.read_status()["session_id"] == "s"


def test_jarvis_decides_a_build_step_off_elijahs_side_of_the_line(build):
    from jarvis.judge import Answer
    m, tg = Memory(":memory:"), FakeTG()
    blocked = {"tool": "Bash", "command": f"touch {build / 'note.txt'}", "cwd": str(build / "src"), "reason": "", "risk": "high"}
    run = runner(agent.Outcome(session_id="s", blocked=blocked), agent.Outcome(session_id="s", text="STATUS: done\nok"))
    yes = lambda state, qs: {q.key: Answer("medium" if q.key == "risk" else True, 0.95) for q in qs}  # noqa: E731
    selfedit.run_agent(m, tg, "x", "build it", runner=run, judge=yes)
    assert (build / "note.txt").exists() and tg.sent == [] and "exit 0" in run.calls[1]["prompt"]


def test_a_permission_prompt_becomes_a_plain_words_question_and_resumes(build):
    m, tg = Memory(":memory:"), FakeTG()
    blocked = {"tool": "Bash", "command": "pip install rich", "cwd": str(build / "src"), "reason": "pip", "risk": "high"}
    run = runner(agent.Outcome(session_id="s", blocked=blocked), agent.Outcome(session_id="s", text="STATUS: done\nok"))

    def answer_after_asked(seconds):  # stands in for the listener: Elijah says yes, JARVIS runs it
        [action] = m.pending_actions(kind="run_command")
        if action["notified_at"]:
            m.resolve_action(action["id"], "done")
            m.set_kv(f"agent_outcome:{action['id']}", "[exit 0]\ninstalled rich")
    import time as _time
    real_sleep = _time.sleep
    _time.sleep = answer_after_asked
    try:
        selfedit.run_agent(m, tg, "Pretty output", "build it", runner=run, poll=0)
    finally:
        _time.sleep = real_sleep
    question = next(text for text, buttons in tg.sent if buttons)
    assert "for the build" in question and "$ pip install rich" in question
    assert run.calls[1]["resume"] == "s" and "installed rich" in run.calls[1]["prompt"]


def test_a_no_is_passed_on(build):
    m, tg = Memory(":memory:"), FakeTG()
    blocked = {"tool": "Bash", "command": "git push", "cwd": str(build / "src"), "reason": "", "risk": "high"}
    run = runner(agent.Outcome(session_id="s", blocked=blocked), agent.Outcome(session_id="s", text="STATUS: stuck\nno"))
    import time as _time
    real_sleep = _time.sleep
    _time.sleep = lambda s: m.resolve_action(m.pending_actions(kind="run_command")[0]["id"], "rejected")
    try:
        selfedit.run_agent(m, tg, "x", "build it", runner=run, poll=0)
    finally:
        _time.sleep = real_sleep
    assert "did not approve" in run.calls[1]["prompt"]


def test_hangs_restart_the_session_then_give_up(build):
    m, tg = Memory(":memory:"), FakeTG()
    run = runner(agent.Outcome(session_id="s", hung=True))
    with pytest.raises(RuntimeError, match="went quiet"):
        selfedit.run_agent(m, tg, "x", "build it", runner=run)
    assert len(run.calls) == selfedit.MAX_RESTARTS + 1 and run.calls[1]["resume"] == "s"


def test_stop(build):
    m, tg = Memory(":memory:"), FakeTG()
    with pytest.raises(RuntimeError, match="stopped by Elijah"):
        selfedit.run_agent(m, tg, "x", "build it", runner=runner(agent.Outcome(session_id="s", stopped=True)))


def test_status_and_idempotent_start(build, monkeypatch):
    m, tg = Memory(":memory:"), FakeTG()
    assert selfedit.status_text() == "no build has run yet"
    spawned = []
    monkeypatch.setattr(selfedit, "spawn", lambda cmd, i=None: spawned.append(cmd))
    assert selfedit.start(m, tg).startswith("nothing queued")
    eid = m.add_self_edit("request", "Status command", "add it")
    m.set_self_edit(eid, status="queued")
    assert selfedit.start(m, tg) == "started" and spawned == ["build"]
    import os
    selfedit._status(pid=os.getpid(), title="Status command", phase="building", last_update="Editing chat.py")
    assert selfedit.start(m, tg).startswith("already running: Status command")
    assert "Editing chat.py" in selfedit.status_text()


def test_tick_restarts_a_dead_builder_twice_then_gives_up(build, monkeypatch):
    m, tg = Memory(":memory:"), FakeTG()
    spawned = []
    monkeypatch.setattr(selfedit, "spawn", lambda cmd, i=None: spawned.append(cmd))
    eid = m.add_self_edit("request", "Status command", "add it")
    for _ in range(selfedit.MAX_RESTARTS):
        m.set_self_edit(eid, status="building")
        assert selfedit.check_health(m, tg) == "restarted"
    m.set_self_edit(eid, status="building")
    assert selfedit.check_health(m, tg) == "gave up" and m.get_self_edit(eid)["status"] == "failed"
    from jarvis import reports
    assert len(spawned) == selfedit.MAX_RESTARTS and reports.recent(m)[0]["kind"] == "failed"


def test_a_silent_builder_is_reported_once(build, monkeypatch):
    import os
    import time
    from jarvis import reports
    m, tg = Memory(":memory:"), FakeTG()
    eid = m.add_self_edit("request", "README note", "add it")
    m.set_self_edit(eid, status="building")
    selfedit._status(pid=os.getpid(), started=time.time() - 3600, phase="building")
    assert selfedit.check_health(m, tg) == "stalled (reported)"
    assert "no progress for 60 minutes" in reports.recent(m)[0]["text"]
    assert selfedit.check_health(m, tg) == "ok"  # once per build


def _repo_pair(tmp_path, monkeypatch):
    import subprocess
    origin, clone = tmp_path / "origin", tmp_path / "src"
    def git(*a, cwd):
        subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True,
                       env={**__import__("os").environ, **selfedit.GIT_ENV})
    origin.mkdir()
    git("init", "-q", "-b", "main", cwd=origin)
    (origin / "a.py").write_text("a = 1\n")
    git("add", "-A", cwd=origin)
    git("commit", "-qm", "base", cwd=origin)
    git("clone", "-q", str(origin), str(clone), cwd=tmp_path)
    git("checkout", "-qb", "self-edit-1", cwd=clone)
    (clone / "b.py").write_text("b = 1\n")
    git("add", "-A", cwd=clone)
    git("commit", "-qm", "build", cwd=clone)
    monkeypatch.setattr(selfedit, "CLONE", clone)
    return origin, clone, git


def test_a_finished_build_catches_up_with_main_and_is_retested(build, tmp_path, monkeypatch):
    origin, clone, git = _repo_pair(tmp_path, monkeypatch)
    ran = []
    monkeypatch.setattr(selfedit, "run_tests", lambda cwd: ran.append(cwd) or (True, "ok"))
    assert selfedit.catch_up_with_main() is None and ran == []  # nothing new on main: no re-run
    (origin / "c.py").write_text("c = 1\n")
    git("add", "-A", cwd=origin)
    git("commit", "-qm", "newer main", cwd=origin)
    assert selfedit.catch_up_with_main() is None and ran == [clone]
    assert (clone / "c.py").exists() and (clone / "b.py").exists()


def test_a_build_that_conflicts_with_newer_main_is_not_offered(build, tmp_path, monkeypatch):
    origin, clone, git = _repo_pair(tmp_path, monkeypatch)
    (origin / "b.py").write_text("b = 2\n")
    git("add", "-A", cwd=origin)
    git("commit", "-qm", "conflicting main", cwd=origin)
    monkeypatch.setattr(selfedit, "run_tests", lambda cwd: (True, "ok"))
    assert "conflicts" in selfedit.catch_up_with_main()
    assert (clone / "b.py").read_text() == "b = 1\n"  # rebase aborted cleanly
