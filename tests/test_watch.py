import json
from datetime import datetime, timedelta, timezone

import pytest

from jarvis import terminal
from jarvis.memory import Memory
from jarvis.repos import check_git, project_for
from jarvis.screen import Window, excluded, meaningfully_changed, redact
from jarvis.watch import Watcher, recent_activity

REPOS = ["/Users/e/Code/shop-site", "/Users/e/Code/JARVIS", "/Users/e/Code/JARVIS/vendor/lib"]


def test_redact_secrets():
    assert redact("export OPENAI_API_KEY=sk-abc123def456") == "export OPENAI_API_KEY=<redacted>"
    assert redact('password: "hunter22"') == "password=<redacted>"
    assert redact("TELEGRAM_BOT_TOKEN=1234567890:FAKEfakeFAKEfake123") == "TELEGRAM_BOT_TOKEN=<redacted>"
    assert "<redacted>" in redact("key ghp_a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6")
    assert redact('export FAL_KEY="fal_sk_x:y"') == "export FAL_KEY=<redacted>"
    assert redact("STRIPE_KEY=abcd") == "STRIPE_KEY=<redacted>"
    assert redact("the keyboard is fine") == "the keyboard is fine"
    assert redact("git commit -m 'fix login page'") == "git commit -m 'fix login page'"
    assert redact("/Users/elijahryerson/Library/Mobile Documents") == "/Users/elijahryerson/Library/Mobile Documents"


def test_exclusions():
    assert excluded("Messages", "Chat with Mom")
    assert excluded("1Password", None)
    assert excluded("Google Chrome", "Chase Online - Account Summary")
    assert excluded("Safari", "Sign in to your account")
    assert not excluded("Discord", "#class-discussion")
    assert not excluded("Code", "app.py — shop-site")


def test_change_detection():
    assert meaningfully_changed(None, "hello")
    assert not meaningfully_changed("same text", "same text")
    base = "line\n" * 100
    assert not meaningfully_changed(base, base + "one more")
    assert meaningfully_changed(base, "totally different content " * 20)


def test_project_for():
    assert project_for(REPOS, cwd="/Users/e/Code/shop-site/src") == "shop-site"
    assert project_for(REPOS, cwd="/Users/e/Code/JARVIS/vendor/lib/x") == "lib"  # deepest repo wins
    assert project_for(REPOS, text="checkout.ts — shop-site — Visual Studio Code") == "shop-site"
    assert project_for(REPOS, cwd="/tmp") is None


class Clock:
    t = 1000.0


def make_watcher(mem, windows, texts, idle=5):
    w = Watcher(mem, log=lambda s: None, front=lambda: windows[0],
                capture=lambda win: texts[0], idle=lambda: idle)
    return w


def test_watcher_saves_changes_and_skips_idle_excluded_and_repeats():
    m = Memory(":memory:")
    m.set_kv("repos", json.dumps(REPOS))
    windows, texts = [Window(1, "Code", "app.py — shop-site")], ["def checkout(): ..."]
    w = make_watcher(m, windows, texts)
    assert w.look(1000).startswith("saved Code")
    assert w.look(1010) == "same window"
    assert w.look(1100) == "unchanged"
    texts[0] = "completely new code " * 30
    assert w.look(1200).startswith("saved")
    (first, second) = m.observations(datetime.now(timezone.utc) - timedelta(minutes=5))
    assert first["project"] == "shop-site" and first["source"] == "screen"

    windows[0] = Window(2, "Google Chrome", "Wells Fargo - Sign On")
    assert w.look(1300) == "excluded window title"
    assert len(m.observations(datetime.now(timezone.utc) - timedelta(minutes=5))) == 2

    idle_w = make_watcher(m, windows, texts, idle=600)
    assert idle_w.look(1400) == "idle"


def test_terminal_ingest(tmp_path):
    m = Memory(":memory:")
    m.set_kv("repos", json.dumps(REPOS))
    log = tmp_path / "terminal.log"
    log.write_text(
        "1760572800\t0\t/Users/e/Code/shop-site\t1.2\tnpm test\n"
        "1760572900\t1\t/Users/e/Code/shop-site\t0.4\texport API_KEY=abcd1234efgh\n"
        "1760573000\t0\t/tmp\t0.0\tpartial line no newline"
    )
    assert terminal.ingest(m, log) == 2
    obs = m.observations(datetime(2020, 1, 1, tzinfo=timezone.utc), source="terminal")
    assert obs[0]["text"] == "$ npm test  [ok, 1.2s]" and obs[0]["project"] == "shop-site"
    assert "API_KEY=<redacted>" in obs[1]["text"] and "exit 1" in obs[1]["text"]
    assert terminal.ingest(m, log) == 0
    with log.open("a") as f:
        f.write("\n")
    assert terminal.ingest(m, log) == 1


def test_install_hook_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal, "HOOK", tmp_path / "hook.zsh")
    monkeypatch.setattr(terminal, "SOURCE_LINE", f'source "{tmp_path / "hook.zsh"}"  # JARVIS terminal sense')
    zshrc = tmp_path / ".zshrc"
    zshrc.write_text("export PATH=$PATH:/x")
    assert terminal.install_hook(zshrc).startswith("added")
    assert terminal.install_hook(zshrc) == "already installed"
    assert zshrc.read_text().count("JARVIS terminal sense") == 1
    assert "add-zsh-hook precmd" in (tmp_path / "hook.zsh").read_text()
    assert terminal.hook_installed(zshrc)


def test_git_events_after_baseline():
    m = Memory(":memory:")
    m.set_kv("repos", json.dumps(REPOS[:2]))
    states = {REPOS[0]: {"branch": "main", "sha": "a", "subject": "init"},
              REPOS[1]: {"branch": "main", "sha": "b", "subject": "x"}}
    assert check_git(m, state_of=lambda r: dict(states[r])) == 0  # baseline
    states[REPOS[0]] = {"branch": "checkout", "sha": "c", "subject": "Add cart"}
    assert check_git(m, state_of=lambda r: dict(states[r])) == 2
    texts = [o["text"] for o in m.observations(datetime(2020, 1, 1, tzinfo=timezone.utc), source="git")]
    assert texts == ["switched to branch checkout", "new commit on checkout: Add cart"]
    assert check_git(m, state_of=lambda r: None) == 0  # unreadable: keep old state, no events
    assert check_git(m, state_of=lambda r: dict(states[r])) == 0


def test_recent_activity_summary():
    m = Memory(":memory:")
    m.observe("screen", "Traceback: ImportError cart", app="Code", title="cart.py — shop-site", project="shop-site")
    m.observe("terminal", "$ pytest  [exit 1, 2.0s]", project="shop-site")
    out = recent_activity(m, 30)
    assert "Code | cart.py — shop-site [shop-site]" in out and "pytest  [exit 1" in out
    assert "untrusted data" in out and "ImportError cart" in out
    assert recent_activity(Memory(":memory:"), 30) == "(nothing observed recently)"


def test_prune():
    m = Memory(":memory:")
    m.observe("screen", "old", at=datetime.now(timezone.utc) - timedelta(days=10))
    m.observe("screen", "new")
    assert m.prune_observations(timedelta(days=7)) == 1
