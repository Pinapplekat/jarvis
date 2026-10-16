import sys
import textwrap

import pytest

from jarvis import warm
from jarvis.warm import WarmClaude

# A stand-in for `claude -p --input-format stream-json`: answers each user message with a
# result event; dies after DIE_AFTER messages when given that argument.
FAKE = textwrap.dedent("""
    import json, sys
    die_after = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    n = 0
    for line in sys.stdin:
        msg = json.loads(line)["message"]["content"]
        n += 1
        if die_after and n > die_after:
            sys.exit(1)
        print(json.dumps({"type": "system", "subtype": "init"}), flush=True)
        reply = {"reply": f"{n}:{msg}", "actions": []}
        print(json.dumps({"type": "result", "is_error": False, "structured_output": reply}), flush=True)
""")


@pytest.fixture
def fake_claude(tmp_path):
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE)
    return lambda *extra: (lambda: [sys.executable, str(script), *extra])


def test_session_answers_and_counts_turns(fake_claude):
    w = WarmClaude("sys", {}, argv_for=fake_claude())
    try:
        assert w.run("sys", "hello")["reply"] == "1:hello"
        assert w.run("sys", "again")["reply"] == "2:again"  # same session remembers its turn count
    finally:
        w.close()


def test_recycles_after_max_turns(fake_claude, monkeypatch):
    monkeypatch.setattr(warm, "MAX_TURNS", 2)
    w = WarmClaude("sys", {}, argv_for=fake_claude())
    try:
        assert [w.run("s", f"m{i}")["reply"] for i in range(3)] == ["1:m0", "2:m1", "1:m2"]  # fresh session
    finally:
        w.close()


def test_only_one_session_until_retirement_is_near(fake_claude, monkeypatch):
    import time
    monkeypatch.setattr(warm, "MAX_TURNS", 3)
    w = WarmClaude("sys", {}, argv_for=fake_claude())
    try:
        w.run("s", "a")
        assert w._next is None  # steady state: just the one in use
        w.run("s", "b")         # turn 2 of 3: spare starts warming
        for _ in range(50):
            if w._next is not None:
                break
            time.sleep(0.05)
        assert w._next is not None
        assert w.run("s", "c")["reply"] == "3:c" and w.run("s", "d")["reply"] == "1:d"
    finally:
        w.close()


def test_dead_session_falls_back_then_recovers(fake_claude):
    fallback_calls, logs = [], []
    w = WarmClaude("sys", {}, argv_for=fake_claude("1"),
                   fallback=lambda s, p: fallback_calls.append(p) or {"reply": "cold", "actions": []},
                   log=logs.append)
    try:
        assert w.run("s", "a")["reply"] == "1:a"
        assert w.run("s", "b")["reply"] == "cold" and fallback_calls == ["b"] and logs
        assert w.run("s", "c")["reply"] == "1:c"  # a new session took over
    finally:
        w.close()
