from datetime import datetime, timedelta, timezone
from itertools import count

import pytest

from jarvis.approvals import handle_updates, notify_pending, parse_reply
from jarvis.memory import Memory
from jarvis.telegram import Update

ME, STRANGER = 111, 999


class FakeTelegram:
    def __init__(self):
        self.sent: list[tuple[str, list | None]] = []
        self.answers: list[str] = []
        self.edits: list[tuple[int, str]] = []
        self.inbox: list[Update] = []
        self._ids = count(1)

    def send(self, text, buttons=None):
        self.sent.append((text, buttons))
        return len(self.sent)

    def answer(self, callback_id, text):
        self.answers.append(text)

    def edit(self, message_id, text):
        self.edits.append((message_id, text))

    def updates(self, offset, wait=0):
        return [u for u in self.inbox if offset is None or u.update_id >= offset]

    def _at(self, at):
        return at or datetime.now(timezone.utc) + timedelta(seconds=1)

    def type(self, text, sender=ME, at=None):
        self.inbox.append(Update(next(self._ids), sender, self._at(at), text=text))

    def press(self, data, sender=ME, message_id=1):
        self.inbox.append(Update(next(self._ids), sender, self._at(None), callback_id="cb",
                                 callback_data=data, message_id=message_id, message_text="draft"))


def _draft(m: Memory, subject="Hi") -> int:
    return m.queue_action("send_email", {
        "from": "me@x.edu", "to": ["prof@x.edu"], "subject": subject, "body": "Hello",
    })


@pytest.mark.parametrize("text,expected", [
    ("YES 3", (True, 3)), ("y3", (True, 3)), ("ok #12", (True, 12)), ("send", (True, None)),
    ("no 4", (False, 4)), ("Cancel 4.", (False, 4)), ("yes please", None), ("maybe", None),
])
def test_parse_reply(text, expected):
    assert parse_reply(text) == expected


def test_drafts_sent_once_with_buttons():
    m, t = Memory(":memory:"), FakeTelegram()
    aid = _draft(m)
    assert notify_pending(m, t) == 1
    text, buttons = t.sent[0]
    assert "prof@x.edu" in text and buttons == [("Approve ✅", f"approve:{aid}"), ("Reject ✖️", f"reject:{aid}")]
    assert notify_pending(m, t) == 0


def test_button_press_approves_and_edits_message():
    m, t = Memory(":memory:"), FakeTelegram()
    aid = _draft(m)
    notify_pending(m, t)
    t.press(f"approve:{aid}")
    assert handle_updates(m, t, ME) == 1
    assert m.get_action(aid)["status"] == "approved"
    assert t.answers[-1].startswith("Approved the email to prof@x.edu")
    assert t.edits and "Approved" in t.edits[-1][1]


def test_typed_bare_yes_needs_exactly_one_open_draft():
    m, t = Memory(":memory:"), FakeTelegram()
    a, b = _draft(m, "A"), _draft(m, "B")
    notify_pending(m, t)
    t.type("yes")
    assert handle_updates(m, t, ME) == 0
    assert "Which one, sir?" in t.sent[-1][0] and "#" not in t.sent[-1][0]
    t.type(f"no {b}")
    assert handle_updates(m, t, ME) == 1
    assert (m.get_action(a)["status"], m.get_action(b)["status"]) == ("pending", "rejected")


def test_each_update_is_handled_once():
    m, t = Memory(":memory:"), FakeTelegram()
    aid = _draft(m)
    notify_pending(m, t)
    t.press(f"reject:{aid}")
    assert handle_updates(m, t, ME) == 1
    assert handle_updates(m, t, ME) == 0
    assert len(t.answers) == 1


def test_ignores_strangers_and_replies_older_than_the_draft():
    m, t = Memory(":memory:"), FakeTelegram()
    t.type("yes 1", at=datetime.now(timezone.utc) - timedelta(hours=1))
    aid = _draft(m)
    notify_pending(m, t)
    t.press(f"approve:{aid}", sender=STRANGER)
    t.type(f"yes {aid}", sender=STRANGER)
    handle_updates(m, t, ME)
    assert m.get_action(aid)["status"] == "pending"


def test_cannot_flip_a_decided_draft():
    m, t = Memory(":memory:"), FakeTelegram()
    aid = _draft(m)
    notify_pending(m, t)
    t.press(f"reject:{aid}")
    t.press(f"approve:{aid}")
    assert handle_updates(m, t, ME) == 1
    assert m.get_action(aid)["status"] == "rejected"
    assert "can't be changed now (rejected)" in t.answers[-1]


def test_ordinary_chat_goes_to_on_text_but_not_from_strangers():
    m, t = Memory(":memory:"), FakeTelegram()
    t.type("what's due this week?")
    t.type("hi, I'm not Elijah", sender=STRANGER)
    seen = []
    assert handle_updates(m, t, ME, on_text=lambda u: seen.append(u.text)) == 0
    assert seen == ["what's due this week?"]
    assert t.sent == []


# ----- confirming in plain words (typed or spoken) -----
from jarvis.approvals import parse_confirmation  # noqa: E402


@pytest.mark.parametrize("text,expected", [
    ("yes", "yes"), ("Yes please.", "yes"), ("yep, go ahead", "yes"), ("do it", "yes"), ("send it", "yes"),
    ("that's what I meant", "yes"), ("ok", "yes"), ("no", "no"), ("nope, don't", "no"), ("cancel", "no"),
    ("hold off", "no"), ("yes but change the subject", "unclear"), ("no wait, send it to Dana", "unclear"),
    ("what's on today?", None), ("stop", None),
])
def test_parse_confirmation(text, expected):
    assert parse_confirmation(text) == expected


def _command(m, argv=("git", "push")):
    from jarvis.terminal import Verdict, request_approval
    return request_approval(m, Verdict("approval", "git push", list(argv), risk="high"), "/tmp", "publish it")


def test_a_typed_yes_runs_the_single_waiting_command_like_its_button():
    m, t = Memory(":memory:"), FakeTelegram()
    aid = _command(m)
    notify_pending(m, t)
    pressed = []
    t.type("yes, go ahead")
    handle_updates(m, t, ME, on_button=lambda verb, i: pressed.append((verb, i)) or "Running it")
    assert pressed == [("cmd_run", aid)]
    assert t.sent[-1][0].startswith("Running git push") and "#" not in t.sent[-1][0]


def test_a_clear_no_cancels_and_an_unclear_answer_cancels_too_then_goes_to_chat():
    m, t = Memory(":memory:"), FakeTelegram()
    aid = _draft(m)
    notify_pending(m, t)
    chatted = []
    t.type("yes but make it shorter")
    handle_updates(m, t, ME, on_text=chatted.append)
    assert m.get_action(aid)["status"] == "rejected"
    assert "held off on the email to prof@x.edu" in t.sent[-1][0] and len(chatted) == 1

    aid2 = _draft(m, "Again")
    notify_pending(m, t)
    t.type("nope")
    handle_updates(m, t, ME)
    assert m.get_action(aid2)["status"] == "rejected" and t.sent[-1][0].startswith("Cancelled the email")


def test_a_spoken_yes_to_an_email_approves_exactly_that_draft():
    m, t = Memory(":memory:"), FakeTelegram()
    aid = _draft(m)
    notify_pending(m, t)
    t.inbox.append(Update(99, ME, datetime.now(timezone.utc) + timedelta(seconds=1), voice_file_id="v1"))
    t.download = lambda file_id: b"ogg"
    t.action = lambda kind: None
    assert handle_updates(m, t, ME, transcribe=lambda audio: "Yes, send it.") == 1
    assert m.get_action(aid)["status"] == "approved" and m.get_kv(f"email_ok:{aid}")


def test_yes_with_several_waiting_asks_which_by_what_they_are():
    m, t = Memory(":memory:"), FakeTelegram()
    _draft(m)
    _command(m)
    notify_pending(m, t)
    t.type("yes")
    handle_updates(m, t, ME, on_button=lambda verb, i: pytest.fail("ran something"))
    assert "Which one, sir?" in t.sent[-1][0] and "the email to prof@x.edu" in t.sent[-1][0]
    assert "running git push" in t.sent[-1][0] and "#" not in t.sent[-1][0]


def test_ok_with_nothing_waiting_is_just_chat():
    m, t = Memory(":memory:"), FakeTelegram()
    chatted = []
    t.type("ok")
    handle_updates(m, t, ME, on_text=chatted.append)
    assert len(chatted) == 1 and t.sent == []


def test_a_yes_written_before_the_question_doesnt_count():
    m, t = Memory(":memory:"), FakeTelegram()
    t.type("yes", at=datetime.now(timezone.utc) - timedelta(minutes=5))
    aid = _command(m)
    notify_pending(m, t)
    handle_updates(m, t, ME, on_text=lambda u: None, on_button=lambda verb, i: pytest.fail("ran it"))
    assert m.get_action(aid)["status"] == "pending"


def test_an_expired_command_cant_be_confirmed():
    from jarvis import config
    m, t = Memory(":memory:"), FakeTelegram()
    aid = _command(m)
    notify_pending(m, t)
    old = (datetime.now(timezone.utc) - timedelta(minutes=config.COMMAND_APPROVAL_MINUTES + 5)).isoformat()
    m.db.execute("UPDATE pending_actions SET created_at = ? WHERE id = ?", (old, aid))
    t.type("yes")
    handle_updates(m, t, ME, on_text=lambda u: None, on_button=lambda verb, i: pytest.fail("ran it"))


def test_drafts_and_commands_ask_is_that_what_you_meant_and_can_say_it():
    m, t = Memory(":memory:"), FakeTelegram()
    _draft(m)
    _command(m)
    spoken = []
    assert notify_pending(m, t, say=spoken.append) == 2
    assert all("Yes or no?" in text or "Is that what you meant" in text for text, _ in t.sent)
    assert all("#" not in text.split("$")[0] for text, _ in t.sent)
    assert len(spoken) == 2


def test_a_yes_after_jarvis_asked_something_else_is_chat_not_a_confirmation():
    import time
    m, t = Memory(":memory:"), FakeTelegram()
    aid = _command(m)
    notify_pending(m, t)
    time.sleep(1.1)  # timestamps are to the second
    m.add_chat("assistant", "Shall I remind you at nine, sir?")
    chatted = []
    t.type("yes")
    handle_updates(m, t, ME, on_text=chatted.append, on_button=lambda verb, i: pytest.fail("ran it"))
    assert len(chatted) == 1 and m.get_action(aid)["status"] == "pending"
