import json
import os
import sys
from datetime import datetime, timedelta, timezone

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
                                     "uv run pytest -q", "pytest tests/test_x.py -k voice"])
def test_read_only_allowlist_runs_automatically(project, command):
    assert classify(command).kind == "auto"


@pytest.mark.parametrize("command", [
    "rm -rf jarvis", "git commit -m x", "git push", "git -c core.pager=evil log", "git diff --output=/tmp/x",
    "git log --ext-diff", "git show HEAD:.env", "cat .env", "grep TOKEN .env", "cat ../secret",
    "cat up/outside.txt", "cat /etc/hosts", "tail -f jarvis/chat.py", "tail -nf 5 x", "grep -rf pats jarvis",
    "pytest -p evil_plugin", "python script.py", "/bin/ls", "curl https://x.test", "uv add requests",
])
def test_everything_else_needs_approval(project, command):
    assert classify(command).kind == "approval"


@pytest.mark.parametrize("command", ["ls | wc", "cat x > y", "ls; rm -rf /", "echo $(whoami)", "ls && rm x",
                                     "cat `id`", "echo $HOME", "ls\nrm x", ""])
def test_shell_syntax_is_rejected_outright(project, command):
    assert classify(command).kind == "reject"


def test_cwd_outside_project_needs_approval(project, tmp_path):
    assert classify("ls", tmp_path).kind == "approval"
    assert classify("ls", project / "jarvis").kind == "auto"


def test_auto_commands_get_safety_flags(project):
    assert "--no-ext-diff" in classify("git diff").argv and "--no-optional-locks" in classify("git status").argv
    assert "--exclude=.env*" in classify("grep -r TOKEN .").argv
    assert classify("uv run pytest -q").argv[:3] == [sys.executable, "-m", "pytest"]


def test_auto_run_is_sandboxed_redacted_and_logged(project):
    (project / "notes.txt").write_text("key sk-ant-" + "z" * 30 + "\n")
    result = terminal.run_command(classify("cat notes.txt"))
    assert result.code == 0 and "sk-ant" not in result.output
    entry = terminal.recent_audit()[-1]
    assert entry["approved_by"] == "auto" and entry["exit"] == 0 and entry["argv"] == ["cat", "notes.txt"]
    assert "zzzz" not in json.dumps(entry)  # the log keeps the length, never the output
    with pytest.raises(ValueError):
        terminal.run_command(classify("rm notes.txt"))  # can't skip approval
    assert (project / "notes.txt").exists()


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def send(self, text, buttons=None):
        self.sent.append((text, buttons))
        return len(self.sent)


def test_approval_flow_runs_only_after_run_is_tapped(project):
    m, tg = Memory(":memory:"), FakeTelegram()
    target = project / "scratch.txt"
    target.write_text("x")
    aid = request_approval(m, classify(f"rm {target}"), None, "you asked me to clean up")
    from jarvis.approvals import notify_pending
    assert notify_pending(m, tg) == 1
    text, buttons = tg.sent[0]
    assert f"$ rm {target}" in text and buttons == [("Run ▶️", f"cmd_run:{aid}"), ("Reject ✖️", f"cmd_reject:{aid}")]
    assert target.exists()  # nothing ran yet
    assert handle_button(m, tg, "cmd_run", aid, background=False) == f"Running #{aid}"
    assert not target.exists() and m.get_action(aid)["status"] == "done"
    assert "exit 0" in tg.sent[-1][0]
    assert handle_button(m, tg, "cmd_run", aid, background=False) == "Already handled"  # never twice
    assert terminal.recent_audit()[-1]["approved_by"] == "telegram"


def test_reject_and_expiry(project):
    m, tg = Memory(":memory:"), FakeTelegram()
    aid = request_approval(m, classify("touch made.txt"), None, "")
    assert handle_button(m, tg, "cmd_reject", aid, background=False) == f"Rejected #{aid}"
    assert m.get_action(aid)["status"] == "rejected" and not (project / "made.txt").exists()

    aid2 = request_approval(m, classify("touch made.txt"), None, "")
    late = datetime.now(timezone.utc) + timedelta(minutes=config.COMMAND_APPROVAL_MINUTES + 1)
    assert handle_button(m, tg, "cmd_run", aid2, background=False, now=late) == "Expired; ask again"
    assert not (project / "made.txt").exists()

    aid3 = request_approval(m, classify("touch made.txt"), None, "")
    assert terminal.expire_commands(m, late) == 1 and m.get_action(aid3)["status"] == "rejected"


def test_typed_yes_never_runs_a_command(project):
    from jarvis.approvals import _decide
    m = Memory(":memory:")
    aid = request_approval(m, classify("touch made.txt"), None, "")
    did, _ = _decide(m, True, aid, datetime.now(timezone.utc) + timedelta(seconds=5))
    assert not did and m.get_action(aid)["status"] == "pending"


def test_timeout_is_reported(project, monkeypatch):
    monkeypatch.setattr(terminal, "APPROVED_TIMEOUT", 0.5)
    v = terminal.Verdict("approval", "", [sys.executable, "-c", "import time; time.sleep(5)"])
    r = terminal.run_command(v, project, approved_by="telegram")
    assert r.timed_out and "timed out" in terminal.describe(r)
    assert terminal.recent_audit()[-1]["timed_out"] is True
