import pytest
from scripted_brain import ScriptedBrain

from jarvis import chat_cli, cli
from jarvis.core.settings import SettingsError, load_settings


def test_ask_prints_the_reply_and_the_tools_used(data_dir, capsys):
    brain = ScriptedBrain([("calculator__evaluate", {"expression": "17 * 2340 / 100"})], text="397.8, sir.")
    assert chat_cli.ask(load_settings(), "what's 17% of 2,340?", brain) == 0
    assert capsys.readouterr().out == "397.8, sir.\n  used: calculator__evaluate\n"
    assert "17% of 2,340" in brain.prompts[0]


def test_ask_fails_loudly_when_the_brain_does(data_dir, capsys):
    assert chat_cli.ask(load_settings(), "hi", ScriptedBrain(error="boom")) == 1
    assert "I couldn't think just now" in capsys.readouterr().out


def test_chat_remembers_earlier_messages(data_dir, capsys, monkeypatch):
    lines = iter(["first thing", "", "second thing", "bye"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(lines))
    brain = ScriptedBrain(text="Noted.")
    assert chat_cli.chat(load_settings(), brain) == 0
    assert len(brain.prompts) == 2  # the empty line is skipped
    assert "Elijah: first thing" in brain.prompts[1] and "You: Noted." in brain.prompts[1]


def test_turns_lists_recent_turns(data_dir, capsys):
    chat_cli.ask(load_settings(), "go", ScriptedBrain([("calculator__evaluate", {"expression": "1+1"})]))
    capsys.readouterr()
    assert cli.main(["turns", "-n", "5"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("#1 ") and " cli " in out and "calculator__evaluate" in out and "$0.0100" in out


def test_core_settings_defaults_and_strictness(data_dir):
    s = load_settings()
    assert (s.model, s.max_turns, s.turn_timeout_seconds) == ("sonnet", 12, 120)
    data_dir.mkdir()
    (data_dir / "settings.toml").write_text('model = "opus"\nmax_turns = 5\nturn_timeout_seconds = 30\n')
    s = load_settings()
    assert (s.model, s.max_turns, s.turn_timeout_seconds) == ("opus", 5, 30)


@pytest.mark.parametrize("line", ["max_turns = 0", 'max_turns = "5"', "model = 3", "turn_timeout_seconds = true"])
def test_core_settings_reject_bad_values(data_dir, line):
    data_dir.mkdir()
    (data_dir / "settings.toml").write_text(line + "\n")
    with pytest.raises(SettingsError):
        load_settings()
