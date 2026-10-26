import asyncio
import json
from dataclasses import replace

import pytest
from scripted_brain import ScriptedBrain

from jarvis.core import actions, context, loop
from jarvis.core.actions import Origin

CLI = Origin("cli")
SEND = ("echo__send", {"text": "hello", "to": "Sam"})


def turn(reg, brain, text="hi", origin=CLI):
    return asyncio.run(loop.handle_message(reg, brain, text, origin))


def turns(reg):
    return [dict(r) for r in reg.conn.execute("SELECT * FROM turns ORDER BY id")]


def chat(reg):
    return [(r["role"], r["text"]) for r in reg.conn.execute("SELECT role, text FROM chat_log ORDER BY id")]


def asks(reg):
    return [dict(r) for r in reg.conn.execute("SELECT * FROM asks ORDER BY id")]


def test_read_and_local_tools_run_and_the_turn_is_recorded(echo):
    brain = ScriptedBrain([("echo__shout", {"text": "hi"}), ("echo__note", {"text": "n"})], text="All done, sir.")
    reply = turn(echo, brain, "do both")
    assert (reply.text, reply.error) == ("All done, sir.", None)
    assert reply.tools == ("echo__shout", "echo__note")
    assert [r.text for r in brain.replies] == ["HI", "Noted n"]
    assert echo.apps["echo"].module.NOTES == ["n"]
    assert chat(echo) == [("user", "do both"), ("assistant", "All done, sir.")]
    (row,) = turns(echo)
    assert (row["channel"], row["model"], row["num_turns"], row["error"]) == ("cli", "sonnet", 3, None)
    assert json.loads(row["tools_json"]) == ["echo__shout", "echo__note"]
    assert row["cost_usd"] == 0.01 and row["ms"] >= 0


def test_tool_failures_are_errors_for_the_model(echo):
    brain = ScriptedBrain([("echo__shout", {}), ("echo__boom", {})])
    turn(echo, brain)
    assert all(r.is_error for r in brain.replies)
    assert "text: required" in brain.replies[0].text
    assert "secret" not in brain.replies[1].text


def test_image_attachment_reaches_the_brain_and_its_file_is_gone(echo):
    brain = ScriptedBrain([("echo__photo", {})])
    turn(echo, brain)
    (image,) = brain.replies[0].images
    assert (image.mime, image.data) == ("image/jpeg", "anBlZw==")  # base64 of b"jpeg"
    assert list((echo.settings.data_dir / "tmp").iterdir()) == []


def test_outbound_waits_then_a_later_turn_approves_it_with_the_stored_args(echo):
    first = ScriptedBrain([SEND])
    turn(echo, first, "tell Sam hello")
    assert first.replies[0].text == "Waiting on Elijah's answer (ask #1): Send hello to Sam?"
    assert not first.replies[0].is_error
    assert echo.apps["echo"].module.SENT == []
    second = ScriptedBrain([("falak__answer_ask", {"ask_id": 1, "yes": True})])
    turn(echo, second, "yeah, send it")
    assert "Pending asks" in second.prompts[0] and "#1 Send hello to Sam?" in second.prompts[0]
    assert echo.apps["echo"].module.SENT == [("hello", "Sam", 1)]
    assert asks(echo)[0]["status"] == "approved"


def test_a_no_declines_the_ask(echo):
    turn(echo, ScriptedBrain([SEND]))
    brain = ScriptedBrain([("falak__answer_ask", {"ask_id": 1, "yes": False})])
    turn(echo, brain, "no, don't")
    assert brain.replies[0].text == "Declined ask #1."
    assert echo.apps["echo"].module.SENT == [] and asks(echo)[0]["status"] == "declined"


def answer(ask_id=1, yes=True):
    return ("falak__answer_ask", {"ask_id": ask_id, "yes": yes})


def refused(echo, brain, origin=CLI):
    turn(echo, brain, "yes", origin)
    assert brain.replies[-1].is_error
    assert echo.apps["echo"].module.SENT == [] and asks(echo)[0]["status"] == "pending"
    return brain.replies[-1].text


def test_answer_ask_guard_channel(echo):
    turn(echo, ScriptedBrain([SEND]))
    assert "own channels" in refused(echo, ScriptedBrain([answer()]), Origin("discord-guest"))


def test_answer_ask_guard_outside_content_earlier_in_the_turn(echo):
    turn(echo, ScriptedBrain([SEND]))
    text = refused(echo, ScriptedBrain([("echo__photo", {}), answer()]))
    assert "outside content" in text


def test_answer_ask_guard_ask_created_during_the_same_turn(echo):
    brain = ScriptedBrain([SEND, ("falak__answer_ask", lambda replies: {"ask_id": 1, "yes": True})])
    turn(echo, brain)
    assert "created during this turn" in brain.replies[-1].text and brain.replies[-1].is_error
    assert echo.apps["echo"].module.SENT == [] and asks(echo)[0]["status"] == "pending"


def test_answer_ask_rejects_bad_arguments_and_unknown_asks(echo):
    brain = ScriptedBrain([("falak__answer_ask", {"ask_id": "1", "yes": True}), answer(99)])
    turn(echo, brain)
    assert [r.is_error for r in brain.replies] == [True, True]
    assert "There is no ask #99" in brain.replies[1].text


def test_untrusted_output_flips_the_origin_for_later_calls_only(echo):
    turn(echo, ScriptedBrain([SEND, ("echo__photo", {}), ("echo__send", {"text": "later", "to": "Al"})]))
    flags = [json.loads(a["origin_json"])["outside_content"] for a in asks(echo)]
    assert flags == [False, True]
    turn(echo, ScriptedBrain([("echo__send", {"text": "next turn", "to": "Al"})]))
    assert json.loads(asks(echo)[-1]["origin_json"])["outside_content"] is False  # a new turn starts clean


def test_other_actions_do_not_flip_the_origin(echo):
    turn(echo, ScriptedBrain([("echo__boom", {}), ("echo__shout", {"text": "x"}), SEND]))
    assert json.loads(asks(echo)[0]["origin_json"])["outside_content"] is False


def test_core_undo_tool(echo):
    brain = ScriptedBrain([("echo__note", {"text": "n"}), ("falak__undo", {}), ("falak__undo", {})])
    turn(echo, brain)
    assert [r.text for r in brain.replies] == ["Noted n", "Un-noted", "There is nothing to undo."]
    assert echo.apps["echo"].module.NOTES == []


def test_prompt_carries_time_history_actions_and_app_context(echo):
    echo.apps["echo"].module.CONTEXT = "Echo knows: sky is blue."
    turn(echo, ScriptedBrain([("echo__shout", {"text": "x"})], text="First reply."), "first message")
    brain = ScriptedBrain()
    turn(echo, brain, "second message")
    prompt = brain.prompts[0]
    assert prompt.startswith("[Context from Falak, not Elijah's words]")
    assert "Channel: cli" in prompt
    assert "Elijah: first message" in prompt and "You: First reply." in prompt
    assert "#1 echo__shout ran" in prompt
    assert "Echo knows: sky is blue." in prompt
    assert prompt.endswith("Elijah says:\nsecond message")
    assert prompt.count("second message") == 1  # the current message isn't also in the history
    assert "Falak" in brain.systems[0] and "{name}" not in brain.systems[0]


def test_history_is_limited_to_the_last_twelve_messages(echo):
    for i in range(10):
        turn(echo, ScriptedBrain(text=f"r{i}"), f"m{i}")
    brain = ScriptedBrain()
    turn(echo, brain, "now")
    assert "m3" not in brain.prompts[0] and "m4" in brain.prompts[0]


def test_failing_context_handler_is_skipped_and_logged(echo, caplog):
    echo.apps["echo"].module.CONTEXT = RuntimeError("broken")
    brain = ScriptedBrain()
    turn(echo, brain)
    assert "broken" not in brain.prompts[0] and brain.replies == []
    assert "context handler of echo failed" in caplog.text


def test_long_context_blocks_are_truncated(echo):
    echo.apps["echo"].module.CONTEXT = "x" * 5000
    (block,) = context.app_blocks(echo)
    assert len(block) == context.APP_BLOCK_MAX and block.endswith("…")


def test_memory_context_lists_recent_facts_without_the_core_naming_memory(real_apps):
    for i in range(25):
        actions.run(real_apps, "memory__remember", {"category": "work", "key": f"k{i}", "value": f"v{i}"}, CLI)
    (block,) = context.app_blocks(real_apps)
    assert block.startswith("What I know about Elijah") and "work/k24: v24" in block
    assert block.count("\n- ") == 20 and "work/k4:" not in block


def test_timeout_gives_the_plain_fallback_and_a_turn_row_with_the_error(echo):
    echo.settings = replace(echo.settings, turn_timeout_seconds=0.05)
    reply = turn(echo, ScriptedBrain(hang=True))
    assert reply.text.startswith("I couldn't think just now, sir (timed out after 0.05s)")
    (row,) = turns(echo)
    assert "timed out" in row["error"]
    assert chat(echo)[-1] == ("assistant", reply.text)


def test_brain_exception_and_brain_error_give_the_fallback(echo):
    first = turn(echo, ScriptedBrain(raises=RuntimeError("no login")))
    second = turn(echo, ScriptedBrain(text="partial", error="error_max_turns: stopped"))
    assert first.text == "I couldn't think just now, sir (RuntimeError: no login)."
    assert second.text == "I couldn't think just now, sir (error_max_turns: stopped)."
    assert [r["error"] for r in turns(echo)] == ["RuntimeError: no login", "error_max_turns: stopped"]


def test_fallback_says_which_tools_already_ran(echo):
    reply = turn(echo, ScriptedBrain([("echo__note", {"text": "n"})], error="boom"))
    assert reply.text.endswith("Before that I ran: echo__note.")
    assert json.loads(turns(echo)[0]["tools_json"]) == ["echo__note"]


def test_a_silent_model_is_an_error_not_an_empty_reply(echo):
    reply = turn(echo, ScriptedBrain(text="  "))
    assert reply.error == "the model said nothing" and reply.text.startswith("I couldn't think")
