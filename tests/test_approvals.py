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

    def updates(self, offset):
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
    assert t.answers[-1].startswith(f"Approved #{aid}")
    assert t.edits and "Approved" in t.edits[-1][1]


def test_typed_bare_yes_needs_exactly_one_open_draft():
    m, t = Memory(":memory:"), FakeTelegram()
    a, b = _draft(m, "A"), _draft(m, "B")
    notify_pending(m, t)
    t.type("yes")
    assert handle_updates(m, t, ME) == 0
    assert "Which one?" in t.sent[-1][0]
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
    assert "can't be changed (rejected)" in t.answers[-1]


def test_ordinary_chat_is_ignored():
    m, t = Memory(":memory:"), FakeTelegram()
    t.type("what's due this week?")
    assert handle_updates(m, t, ME) == 0
    assert t.sent == []
