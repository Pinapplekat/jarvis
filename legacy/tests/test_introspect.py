import os
import sys
from pathlib import Path

import pytest

from jarvis import config, introspect
from jarvis.introspect import Refused

BOT_TOKEN = "123456789:" + "A" * 35  # shaped like a Telegram token; not a real one


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    (root / "jarvis").mkdir(parents=True)
    (root / "jarvis" / "chat.py").write_text("def respond():\n    return 'hi'\n")
    (root / "README.md").write_text("# JARVIS\nRead-only inspection.\n")
    (root / ".env").write_text(f"TELEGRAM_BOT_TOKEN={BOT_TOKEN}\nCANVAS_ICS_URL=https://c.test/feeds/calendars/user_x.ics\n")
    (root / ".env.example").write_text("TELEGRAM_BOT_TOKEN=\n")
    (root / "token_cache.json").write_text("{}")
    (root / "blob.bin").write_bytes(b"\0\1\2binary")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]")
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    os.symlink(outside, root / "jarvis" / "escape.txt")
    monkeypatch.setattr(config, "PROJECT_DIR", root)
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    return root


def test_reads_project_files_with_line_numbers(project):
    out = introspect.read_project_file("jarvis/chat.py")
    assert "   1  def respond():" in out and "(2 lines in all)" in out
    ranged = introspect.read_project_file("jarvis/chat.py:2-2")
    assert "   2      return 'hi'" in ranged and "def respond" not in ranged
    assert "Read-only" in introspect.inspect("read", "README.md")
    assert "# JARVIS" in introspect.read_project_file(str(project / "README.md"))  # absolute inside is fine


@pytest.mark.parametrize("path", ["../outside.txt", "jarvis/../../outside.txt", "/etc/hosts",
                                  "jarvis/escape.txt", "~/.ssh/id_rsa"])
def test_path_escapes_are_refused(project, path):
    with pytest.raises(Refused):
        introspect.read_project_file(path)


@pytest.mark.parametrize("path", [".env", "token_cache.json", ".git/config", "blob.bin", "nope.py"])
def test_secrets_git_internals_binary_and_missing_files_are_refused(project, path):
    with pytest.raises(Refused):
        introspect.read_project_file(path)


def test_env_example_is_readable_and_listing_skips_secrets(project):
    assert "TELEGRAM_BOT_TOKEN=" in introspect.read_project_file(".env.example")
    listing = introspect.list_files()
    assert "jarvis/chat.py" in listing and "README.md" in listing
    assert ".env " not in listing and "token_cache" not in listing and ".git" not in listing
    assert "escape.txt" not in listing  # symlinks aren't followed


def test_search_finds_code_but_never_secret_files(project):
    out = introspect.search_code("respond")
    assert "jarvis/chat.py:1:" in out
    assert "no matches" in introspect.search_code("TELEGRAM_BOT_TOKEN=1234")
    with pytest.raises(Refused):
        introspect.search_code("x")


def test_redaction_masks_known_values_and_token_shapes(project):
    text = (f"token {BOT_TOKEN} feed https://canvas.test/feeds/calendars/user_abc.ics "
            "twilio AC" + "0" * 32 + " key sk-ant-" + "b" * 30)
    out = introspect.redact(text)
    assert BOT_TOKEN not in out and "feeds/calendars" not in out and "AC000" not in out and "sk-ant" not in out
    assert out.count("<redacted>") >= 4


def test_truncation():
    out = introspect.redact("x" * 10_000, limit=100)
    assert len(out) < 200 and "cut; 9900 more" in out


def test_logs_only_known_names(project, tmp_path, monkeypatch):
    monkeypatch.setattr(introspect, "LOG_DIR", tmp_path)
    (tmp_path / "ears.log").write_text("\n".join(f"line {i}" for i in range(100)) + f"\nleak {BOT_TOKEN}\n")
    out = introspect.tail_log("ears", 3)
    assert out.splitlines()[0] == "line 98" and BOT_TOKEN not in out
    with pytest.raises(Refused):
        introspect.tail_log("../../etc/passwd")
    assert "empty" in introspect.tail_log("tick")


def test_checks_are_a_fixed_allowlist(project):
    with pytest.raises(Refused):
        introspect.run_check("rm -rf /")
    with pytest.raises(Refused):
        introspect.inspect("exec", "ls")


def test_run_argv_times_out_and_scrubs_env(project, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    r = introspect.run_argv([sys.executable, "-c", "import time; time.sleep(5)"], project, timeout=0.5,
                            sandboxed=False)
    assert r.timed_out and r.code is None
    r = introspect.run_argv([sys.executable, "-c", "import os; print(sorted(os.environ))"], project, sandboxed=False)
    assert "TELEGRAM_BOT_TOKEN" not in r.output and "PATH" in r.output


@pytest.mark.skipif(not os.path.exists("/usr/bin/sandbox-exec"), reason="macOS sandbox")
def test_sandbox_blocks_writes_and_network(project):
    probe = Path(__file__).with_name("_sandbox_probe.txt")  # not under a temp dir, which is allowed
    try:
        r = introspect.run_argv(["touch", str(probe)], project)
        assert r.code != 0 and not probe.exists()
    finally:
        probe.unlink(missing_ok=True)
    r = introspect.run_argv([sys.executable, "-c",
                             "import socket; socket.create_connection(('1.1.1.1', 53), timeout=2)"], project)
    assert r.code != 0
    r = introspect.run_argv(["cat", "README.md"], project)
    assert r.code == 0 and "JARVIS" in r.output
