"""JARVIS never says it did something it didn't (the overnight review's examples)."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from jarvis import config, honesty
from jarvis.chat import respond
from jarvis.memory import Memory
from jarvis.warm import early_actions

TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 18, 15, 0, tzinfo=TZ)


@pytest.mark.parametrize("sentence,kinds,pending,busy", [
    ("Playing study lo-fi now.", set(), False, False),           # promise, no action
    ("Draft is above, sir.", set(), False, False),
    ("The command is waiting in Telegram.", set(), False, False),
    ("The build is running.", set(), False, False),
    ("Redoing it now.", set(), False, False),
    ("I'm checking your inbox.", set(), False, False),
    ("I'll send it to her.", set(), False, False),
    ("Having a look, sir.", set(), False, False),                 # a look promised with no look
    ("I'm taking a look at it now.", set(), False, False),
    ("Taking a quick look, sir.", set(), False, False),
    ("Let me see, sir.", set(), False, False),
])
def test_unbacked_claims_are_caught(sentence, kinds, pending, busy):
    assert honesty.is_unbacked(sentence, kinds, pending, busy)


@pytest.mark.parametrize("sentence,kinds,pending,busy", [
    ("Putting it on, sir.", {"play_music"}, False, False),
    ("Having a look, sir.", {"look_at_screen"}, False, False),    # the look really is happening
    ("Draft is above, sir.", {"draft_email"}, False, False),
    ("It's waiting in Telegram for your OK.", set(), True, False),   # something really is waiting
    ("The build is running.", set(), False, True),                   # a task really is running
    ("Module 3 is due Sunday at 11:59.", set(), False, False),        # plain facts are fine
    ("Nothing due until Sunday, sir.", set(), False, False),
])
def test_backed_or_plain_sentences_pass(sentence, kinds, pending, busy):
    assert not honesty.is_unbacked(sentence, kinds, pending, busy)


def test_failed_action_drops_success_claims_and_says_why():
    text, dropped = honesty.clean("Playing study lo-fi now, sir. Econ is due Sunday.", {"play_music"},
                                  ["play_music: no active Spotify device"])
    assert "Playing" not in text and "Econ is due Sunday." in text
    assert text.endswith("I couldn't start the music: no active Spotify device.")
    assert dropped == ["Playing study lo-fi now, sir."]


def test_failure_from_revise_plan_is_plain():
    text, _ = honesty.clean("Redoing it now.", {"revise_plan"}, ["revise_plan: no plan #4 waiting for approval"])
    assert text == "Sorry, sir. I couldn't revise the plan: no plan #4 waiting for approval."


def test_gate_passes_complete_backed_sentences_only():
    out = []
    g = honesty.Gate(out.append)
    g.actions_known([])
    g.feed("Econ is due Sunday. I'm checking your")
    g.feed("Econ is due Sunday. I'm checking your inbox now. Anything else is quiet")
    g.feed("Econ is due Sunday. I'm checking your inbox now. Anything else is quiet. ")
    assert out == ["Econ is due Sunday. ", "Econ is due Sunday. Anything else is quiet. "]


def test_gate_stops_after_a_failure():
    out = []
    g = honesty.Gate(out.append)
    g.failed = True
    g.feed("Playing it now. ")
    assert out == []


def test_early_actions_parse_once_reply_starts():
    assert early_actions('{"actions": [{"type": "play_music", "query": "lo') is None
    assert early_actions('{"actions": [{"type": "play_music", "query": "lofi"}], "reply": "Put') == [
        {"type": "play_music", "query": "lofi"}]
    assert early_actions('{"reply": "hi", "actions": []') is None  # reply first: not known early


def streaming_runner(actions, reply):
    """A runner that streams like the warm session: actions first, then the reply text."""
    prompts = []

    def run(system, prompt, on_text=None, on_actions=None):
        prompts.append(prompt)
        if on_actions:
            on_actions(actions)
        if on_text:
            for i in range(1, len(reply) + 1):
                on_text(reply[:i])
        return {"actions": actions, "reply": reply}
    run.prompts = prompts
    return run


def test_failed_music_is_never_said_to_be_playing(monkeypatch):
    from jarvis import siri
    monkeypatch.setattr(config, "SIRI", True)

    def broken(mem, query, kind):
        raise ValueError("no active Spotify device")
    monkeypatch.setattr(siri, "music", broken)
    streamed = []
    reply = respond(Memory(":memory:"), "play study lofi", NOW, "",
                    runner=streaming_runner([{"type": "play_music", "query": "study lo-fi"}],
                                            "Playing study lo-fi now, sir. "),
                    on_text=streamed.append)
    assert streamed == []  # the failure was known before anything was said
    assert reply == "Sorry, sir. I couldn't start the music: no active Spotify device."


def test_music_that_starts_is_confirmed_by_the_real_result(monkeypatch):
    from jarvis import siri
    monkeypatch.setattr(config, "SIRI", True)
    calls = []
    monkeypatch.setattr(siri, "music", lambda mem, q, k: calls.append(q) or "Playing Lofi Girl on Spotify.")
    streamed = []
    reply = respond(Memory(":memory:"), "play study lofi", NOW, "",
                    runner=streaming_runner([{"type": "play_music", "query": "study lo-fi"}],
                                            "Putting it on, sir. "),
                    on_text=streamed.append)
    assert calls == ["study lo-fi"]  # done once, early, not again at the end
    assert streamed == ["Putting it on, sir. "]
    assert reply == "Putting it on, sir.\n\nPlaying Lofi Girl on Spotify."


def test_a_bare_promise_gets_one_retry_for_a_real_answer():
    m = Memory(":memory:")
    replies = iter([{"actions": [], "reply": "I'm drafting it now, sir."},
                    {"actions": [], "reply": "I haven't drafted anything yet, sir. Who should it go to?"}])
    prompts = []

    def run(system, prompt):
        prompts.append(prompt)
        return next(replies)
    reply = respond(m, "email my prof", NOW, "", runner=run)
    assert reply == "I haven't drafted anything yet, sir. Who should it go to?"
    assert "had no action that does it" in prompts[1]
    assert any(e["kind"] == "unbacked_claim" for e in m.recent_events())


def test_a_long_answer_starting_with_looking_is_kept():
    assert not honesty.is_unbacked("Looking at the error, it's a missing import in chat.py line 40.", set())


def test_saying_it_checked_after_a_real_lookup_is_kept(monkeypatch):
    from jarvis import introspect
    monkeypatch.setattr(introspect, "inspect", lambda op, arg: "all 830 passed")
    outs = iter([{"actions": [{"type": "inspect", "op": "check", "arg": "tests"}], "reply": "Checking, sir."},
                 {"actions": [], "reply": "I've checked: all the tests pass, sir."}])
    reply = respond(Memory(":memory:"), "do the tests pass?", NOW, "", runner=lambda s, p: next(outs))
    assert reply == "I've checked: all the tests pass, sir."


def test_screen_text_streams_word_by_word_but_never_an_unbacked_promise():
    from jarvis.honesty import Gate
    spoken, shown = [], []
    g = Gate(spoken.append, display=shown.append)
    g.feed("Two things are")
    assert shown == [] and spoken == []  # actions not known yet: nothing half-written shows
    g.actions_known([])
    g.feed("Two things are due")
    assert shown[-1] == "Two things are due" and spoken == []  # on screen at once; spoken waits for the sentence
    g.feed("Two things are due today. I'm checking")
    assert shown[-1] == "Two things are due today." and spoken == ["Two things are due today. "]
