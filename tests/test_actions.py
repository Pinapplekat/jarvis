import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from jarvis.core import actions
from jarvis.core.apps import sweep_tmp
from jarvis.core.actions import Origin

CLI = Origin("cli")
OUTSIDE = Origin("web", outside_content=True)


def run(reg, action, args, origin=CLI, **kw):
    return actions.run(reg, f"echo__{action}", args, origin, **kw)


def status(reg, n=1):
    return [r["status"] for r in actions.recent_log(reg, n)]


def test_needs_confirmation_table():
    f = actions.needs_confirmation
    assert [f(e, CLI, False) for e in ("read", "local", "outbound", "irreversible", "plan")] == [False, False, True, True, True]
    assert f("outbound", CLI, True) is False
    assert f("outbound", OUTSIDE, True) is True
    assert f("irreversible", CLI, True) is True
    assert f("plan", CLI, True) is True


def test_read_and_local_run(echo):
    assert run(echo, "shout", {"text": "hi"}).result.text == "HI"
    assert isinstance(run(echo, "note", {"text": "n"}), actions.Ran)
    assert echo.apps["echo"].module.NOTES == ["n"]


def test_invalid_input_fails_without_calling_handler(echo):
    out = run(echo, "shout", {})
    assert isinstance(out, actions.Failed) and "text: required" in out.message
    assert status(echo) == ["failed"]


def test_unknown_action(echo):
    assert isinstance(run(echo, "nope", {}), actions.Failed)


def test_outbound_asks_then_yes_runs_stored_args_exactly(echo):
    out = run(echo, "send", {"text": "hello", "to": "Sam"})
    assert out == actions.Asked(1, "Send hello to Sam?")
    assert echo.apps["echo"].module.SENT == []
    ran = actions.answer_ask(echo, out.ask_id, True)
    assert isinstance(ran, actions.Ran)
    assert echo.apps["echo"].module.SENT == [("hello", "Sam", 1)]  # the default was stored too
    assert status(echo, 2) == ["asked", "ran"]


def test_decline_does_not_run(echo):
    out = run(echo, "send", {"text": "hello", "to": "Sam"})
    assert isinstance(actions.answer_ask(echo, out.ask_id, False), actions.Declined)
    assert echo.apps["echo"].module.SENT == []
    assert status(echo, 2) == ["asked", "declined"]


def test_ask_answered_once_only(echo):
    out = run(echo, "send", {"text": "a", "to": "b"})
    actions.answer_ask(echo, out.ask_id, True)
    again = actions.answer_ask(echo, out.ask_id, True)
    assert isinstance(again, actions.Failed) and "already approved" in again.message
    assert len(echo.apps["echo"].module.SENT) == 1


def test_expired_ask_cannot_be_approved(echo):
    out = run(echo, "send", {"text": "a", "to": "b"})
    later = datetime.now(timezone.utc) + timedelta(hours=25)
    res = actions.answer_ask(echo, out.ask_id, True, now=later)
    assert isinstance(res, actions.Failed) and "expired" in res.message
    assert echo.apps["echo"].module.SENT == []
    assert actions.pending_asks(echo) == []


def test_pending_asks_lists_only_pending(echo):
    a = run(echo, "send", {"text": "a", "to": "b"})
    run(echo, "send", {"text": "c", "to": "d"})
    actions.answer_ask(echo, a.ask_id, False)
    assert [r["id"] for r in actions.pending_asks(echo)] == [2]


def test_allowed_set_lets_outbound_run_without_outside_content(echo):
    allowed = {"echo__send"}
    assert isinstance(run(echo, "send", {"text": "a", "to": "b"}, allowed=allowed), actions.Ran)
    assert isinstance(run(echo, "send", {"text": "a", "to": "b"}, OUTSIDE, allowed=allowed), actions.Asked)


def test_irreversible_always_asks(echo):
    assert isinstance(run(echo, "wipe", {"what": "x"}, allowed={"echo__wipe"}), actions.Asked)
    assert echo.apps["echo"].module.WIPED == []


def test_handler_crash_is_plain_and_logged(echo, caplog):
    out = run(echo, "boom", {})
    assert out == actions.Failed("Something went wrong in echo")
    assert "RuntimeError" in caplog.text and status(echo) == ["failed"]


def test_log_hides_args_when_asked(echo):
    run(echo, "send", {"text": "private", "to": "Sam"})
    run(echo, "shout", {"text": "public"})
    log = actions.recent_log(echo, 2)
    assert log[0]["args"] == "(not logged)" and "private" not in json.dumps(log)
    assert "public" in log[1]["args"]


def test_log_result_truncated(echo):
    run(echo, "shout", {"text": "x" * 900})
    assert len(actions.recent_log(echo)[0]["result"]) == 500


# ---- undo
def test_undo_latest_then_given_id_then_twice(echo):
    run(echo, "note", {"text": "one"})
    run(echo, "note", {"text": "two"})
    notes = echo.apps["echo"].module.NOTES
    assert isinstance(actions.undo(echo), actions.Ran) and notes == ["one"]  # most recent first
    first = next(r["id"] for r in actions.recent_log(echo, 10) if r["tool"] == "echo__note")
    assert isinstance(actions.undo(echo, first), actions.Ran) and notes == []
    again = actions.undo(echo, first)
    assert isinstance(again, actions.Failed) and "already undone" in again.message
    assert "nothing to undo" in actions.undo(echo).message


def test_undo_rejects_non_undoable_and_unknown(echo):
    run(echo, "shout", {"text": "x"})
    assert "can't be undone" in actions.undo(echo, 1).message
    assert "no log entry" in actions.undo(echo, 99).message


# ---- attachments
def test_attachments_released_and_swept(echo):
    out = run(echo, "photo", {})
    (att,) = out.result.attachments
    assert att.path.exists() and oct(att.path.stat().st_mode & 0o777) == "0o600"
    assert oct(att.path.parent.stat().st_mode & 0o777) == "0o700"
    assert "jpeg" not in json.dumps(actions.recent_log(echo))
    actions.release(out)
    assert not att.path.exists()


def test_sweep_removes_only_old_files(echo):
    tmp = echo.settings.data_dir / "tmp"
    old, fresh = tmp / "old.jpg", tmp / "fresh.jpg"
    old.write_bytes(b"x")
    fresh.write_bytes(b"x")
    os.utime(old, (time.time() - 700, time.time() - 700))
    assert sweep_tmp(echo.settings) == 1
    assert not old.exists() and fresh.exists()


def test_startup_sweeps(settings):
    from jarvis.core.apps import load_apps, tmp_dir

    stale = tmp_dir(settings) / "stale.jpg"
    stale.write_bytes(b"x")
    os.utime(stale, (time.time() - 700, time.time() - 700))
    load_apps(settings).close()
    assert not stale.exists()


def test_attachment_outside_tmp_is_refused_not_deleted(echo, tmp_path):
    mod = echo.apps["echo"].module
    precious = tmp_path / "precious.txt"
    precious.write_text("keep")
    original = mod.photo

    def bad(ctx):
        return mod.Result("x", attachments=(mod.Attachment("image", precious, "image/jpeg"),))

    mod.photo = bad
    try:
        out = run(echo, "photo", {})
    finally:
        mod.photo = original
    assert isinstance(out, actions.Failed) and precious.exists()


def test_entries_without_a_subject_undo_in_any_order(echo):
    first = run(echo, "note", {"text": "a"})
    run(echo, "note", {"text": "b"})
    assert isinstance(actions.undo(echo, first.log_id), actions.Ran)


def test_a_plan_asks_every_time_with_the_plan_in_the_question_even_with_a_learned_rule(echo):
    for _ in range(2):
        out = run(echo, "project", {"plan": "add a README"}, allowed={"echo__project"})
        assert isinstance(out, actions.Asked) and out.question == "Here's my plan: add a README. Go?"
    assert echo.apps["echo"].module.WIPED == []
    done = actions.answer_ask(echo, out.ask_id, True)
    assert isinstance(done, actions.Ran) and echo.apps["echo"].module.WIPED == ["project:add a README"]


def test_confirm_shows_a_list_of_words_as_words():
    got = actions._plain({"steps": ['press "Go"', 'check "Agree"'], "n": 2, "mixed": [1, 2]})
    assert "{steps}".format_map(got) == 'press "Go"; check "Agree"'
    assert "{n}".format_map(got) == "2" and "{missing}".format_map(got) == "(not given)"
