import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from jarvis import config, terminal
from jarvis.memory import Memory
from jarvis.terminal import classify, handle_button, request_approval

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS commands")


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    (root / "jarvis").mkdir(parents=True)
    (root / "jarvis" / "chat.py").write_text("def respond():\n    pass\n")
    (root / ".env").write_text("TELEGRAM_BOT_TOKEN=x\n")
    os.symlink(tmp_path, root / "up")
    monkeypatch.setattr(config, "PROJECT_DIR", root)
    monkeypatch.setattr(terminal, "AUDIT", tmp_path / "commands.jsonl")
    return root


@pytest.mark.parametrize("command", ["ls", "ls -la jarvis", "cat jarvis/chat.py", "head -n 5 jarvis/chat.py",
                                     "wc -l jarvis/chat.py", "grep -rn respond jarvis", "git status",
                                     "git log -5 --oneline", "git diff --stat", "git show HEAD",
                                     "uv run pytest -q", "pytest tests/test_x.py -k voice",
                                     # reads anywhere on the Mac are low risk now
                                     "cat /etc/hosts", "cat up/outside.txt", "ls ~", "find ~ -name x", "cd .."])
def test_low_risk_reads_run_automatically_anywhere(project, command):
    v = classify(command)
    assert (v.kind, v.risk, v.sandbox) == ("auto", "low", "read"), v.reason


@pytest.mark.parametrize("command", ["git commit -m x", "git add jarvis/chat.py", "touch made.txt", "mkdir build",
                                     "rm notes.txt", "mv a.txt b.txt", "python script.py", "uv add requests",
                                     "uv sync", "uv run --extra dev pytest -q", "npm install"])
def test_medium_risk_project_work_runs_automatically_in_the_write_sandbox(project, command):
    v = classify(command)
    assert (v.kind, v.risk) == ("auto", "medium"), v.reason
    assert v.sandbox in ("write", "git") and v.root == str(project.resolve())


@pytest.mark.parametrize("command,risk", [
    ("rm -rf jarvis", "high"), ("git push", "high"), ("git push --force", "critical"), ("git reset --hard", "high"),
    ("git -c core.pager=evil log", "high"), ("git diff --output=/tmp/x", "high"), ("git log --ext-diff", "high"),
    ("tail -f jarvis/chat.py", "high"), ("tail -nf 5 x", "high"), ("grep -rf pats jarvis", "high"),
    ("pytest -p evil_plugin", "high"), ("/bin/ls", "high"), ("curl https://x.test", "high"), ("pip install x", "high"),
    ("touch /usr/local/x", "high"), ("python /etc/x.py", "high"), ("brew install x", "high"),
    ("sudo ls", "critical"), ("rm -rf ~/Documents", "critical"), ("osascript -e x", "critical"),
    ("launchctl unload x", "critical"), ("uv run rm -rf build", "high"), ("frobnicate", "high"),
])
def test_high_and_critical_wait_for_elijah_and_unknown_is_high(project, command, risk):
    v = classify(command)
    assert (v.kind, v.risk) == ("approval", risk), v.reason


@pytest.mark.parametrize("command", ["git show HEAD:.env", "cat .env", "grep TOKEN .env", "cat ~/.ssh/id_rsa",
                                     "ls ~/.ssh", "cat ~/Library/Keychains/login.keychain-db", "rm .env",
                                     "cp .env /tmp/x"])
def test_secrets_are_refused_whatever_the_grade(project, command):
    assert classify(command).kind == "reject"


def test_secret_folders_are_refused_as_the_working_folder_too(project):
    assert classify("ls", Path.home() / ".ssh").kind == "reject"


@pytest.mark.parametrize("command", ["ls | wc", "cat x > y", "ls; rm -rf /", "echo $(whoami)", "ls && rm x",
                                     "cat `id`", "echo $HOME", "ls\nrm x", ""])
def test_shell_syntax_is_rejected_outright(project, command):
    assert classify(command).kind == "reject"


def test_outside_the_project_reads_run_and_writes_wait(project, tmp_path):
    assert classify("ls", tmp_path).kind == "auto"
    assert classify("touch x", tmp_path).kind == "approval"
    assert classify("touch x", project / "jarvis").kind == "auto"


def test_auto_commands_get_safety_flags(project):
    assert "--no-ext-diff" in classify("git diff").argv and "--no-optional-locks" in classify("git status").argv
    assert "--exclude=.env*" in classify("grep -r TOKEN .").argv and "--exclude-dir=.ssh" in classify("grep -r x ~").argv
    assert classify("uv run pytest -q").argv[:3] == [sys.executable, "-m", "pytest"]
    assert classify("ls ~/Downloads").argv == ["ls", str(Path.home() / "Downloads")]  # no shell, so JARVIS expands ~


def test_auto_run_is_sandboxed_redacted_and_logged(project):
    (project / "notes.txt").write_text("key sk-ant-" + "z" * 30 + "\n")
    result = terminal.run_command(classify("cat notes.txt"))
    assert result.code == 0 and "sk-ant" not in result.output
    entry = terminal.recent_audit()[-1]
    assert entry["approved_by"] == "auto" and entry["exit"] == 0 and entry["argv"] == ["cat", "notes.txt"]
    assert entry["risk"] == "low"
    assert "zzzz" not in json.dumps(entry)  # the log keeps the length, never the output
    with pytest.raises(ValueError):
        terminal.run_command(classify("rm -r jarvis"))  # can't skip approval
    assert (project / "jarvis").exists()


def test_the_read_sandbox_cant_open_env_files_even_by_another_route(project):
    v = terminal.Verdict("auto", "test", [sys.executable, "-c", "print(open('.env').read())"], risk="low")
    result = terminal.run_command(v, project)
    assert result.code != 0 and "TELEGRAM_BOT_TOKEN" not in result.output


def test_medium_commands_write_inside_the_project_only(project):
    terminal.run_command(classify("touch made.txt"))
    assert (project / "made.txt").exists()
    outside = Path.home() / f".jarvis-sandbox-test-{os.getpid()}"
    v = classify(f"python3 -c open({str(outside)!r},'w')")
    assert (v.kind, v.risk) == ("auto", "medium")
    try:
        result = terminal.run_command(v)
        assert result.code != 0 and not outside.exists()
    finally:
        outside.unlink(missing_ok=True)
    (project / ".git").mkdir()
    r = terminal.run_command(terminal.Verdict("auto", "t", ["touch", ".git/x"], risk="medium", sandbox="write",
                                              root=str(project)))
    assert r.code != 0 and not (project / ".git" / "x").exists()  # only git itself may write .git


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def send(self, text, buttons=None):
        self.sent.append((text, buttons))
        return len(self.sent)


def test_approval_flow_runs_only_after_run_is_tapped(project):
    m, tg = Memory(":memory:"), FakeTelegram()
    target = project / "build"
    (target / "sub").mkdir(parents=True)
    aid = request_approval(m, classify(f"rm -r {target}"), None, "you asked me to clean up")
    from jarvis.approvals import notify_pending
    spoken = []
    assert notify_pending(m, tg, say=spoken.append) == 1
    text, buttons = tg.sent[0]
    assert f"$ rm -r {target}" in text and buttons == [("Run ▶️", f"cmd_run:{aid}"), ("Reject ✖️", f"cmd_reject:{aid}")]
    assert "Is that what you meant?" in text and "high risk" in text and f"#{aid}" not in text
    assert spoken and "is that what you meant" in spoken[0]
    assert target.exists()  # nothing ran yet
    assert handle_button(m, tg, "cmd_run", aid, background=False) == "Running it"
    assert not target.exists() and m.get_action(aid)["status"] == "done"
    assert "exit 0" in tg.sent[-1][0] and "#" not in tg.sent[-1][0]
    assert handle_button(m, tg, "cmd_run", aid, background=False) == "Already handled"  # never twice
    assert terminal.recent_audit()[-1]["approved_by"] == "telegram"


def test_reject_and_expiry(project, tmp_path):
    m, tg = Memory(":memory:"), FakeTelegram()
    made = tmp_path / "made.txt"
    aid = request_approval(m, classify(f"touch {made}"), None, "")
    assert handle_button(m, tg, "cmd_reject", aid, background=False) == "Rejected"
    assert m.get_action(aid)["status"] == "rejected" and not made.exists()

    aid2 = request_approval(m, classify(f"touch {made}"), None, "")
    late = datetime.now(timezone.utc) + timedelta(minutes=config.COMMAND_APPROVAL_MINUTES + 1)
    assert handle_button(m, tg, "cmd_run", aid2, background=False, now=late) == "Expired; ask again"
    assert not made.exists()

    aid3 = request_approval(m, classify(f"touch {made}"), None, "")
    assert terminal.expire_commands(m, late) == 1 and m.get_action(aid3)["status"] == "rejected"


def test_typed_yes_to_an_email_never_runs_a_command(project, tmp_path):
    from jarvis.approvals import _decide
    m = Memory(":memory:")
    aid = request_approval(m, classify(f"touch {tmp_path / 'made.txt'}"), None, "")
    did, _ = _decide(m, True, aid, datetime.now(timezone.utc) + timedelta(seconds=5))
    assert not did and m.get_action(aid)["status"] == "pending"


def test_timeout_is_reported(project, monkeypatch):
    monkeypatch.setattr(terminal, "APPROVED_TIMEOUT", 0.5)
    v = terminal.Verdict("approval", "", [sys.executable, "-c", "import time; time.sleep(5)"])
    r = terminal.run_command(v, project, approved_by="telegram")
    assert r.timed_out and "timed out" in terminal.describe(r)
    assert terminal.recent_audit()[-1]["timed_out"] is True


def test_background_run_uses_its_own_db_connection(project, tmp_path):
    import time
    m, tg = Memory(tmp_path / "jarvis.db"), FakeTelegram()
    target = project / "build"
    target.mkdir()
    aid = request_approval(m, classify(f"rm -r {target}"), None, "")
    assert handle_button(m, tg, "cmd_run", aid) == "Running it"  # background thread, file db
    for _ in range(200):
        if m.get_action(aid)["status"] != "approved":
            break
        time.sleep(0.02)
    assert m.get_action(aid)["status"] == "done" and not target.exists()
    assert "exit 0" in tg.sent[-1][0]


def test_ps_works_even_though_the_sandbox_cant_launch_it(project):
    v = classify("ps -o pid,etime -p 1")
    assert v.kind == "auto" and not v.sandboxed and classify("pgrep -fl python").sandboxed
    result = terminal.run_command(v)
    assert result.code == 0 and "PID" in result.output
