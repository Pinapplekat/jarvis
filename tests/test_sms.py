from datetime import datetime, timedelta, timezone
from itertools import count

import pytest

from jarvis.memory import Memory
from jarvis.sms import InboundSms, handle_replies, notify_pending, parse_reply

ME = "+12065550100"
STRANGER = "+12065559999"


class FakeTwilio:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.inbox: list[InboundSms] = []
        self._ids = count(1)

    def send(self, to, body):
        self.sent.append((to, body))

    def inbound(self, since):
        return [s for s in self.inbox if s.sent_at >= since]

    def reply(self, body, sender=ME, at=None):
        at = at or datetime.now(timezone.utc) + timedelta(seconds=1)
        self.inbox.append(InboundSms(f"SM{next(self._ids)}", sender, body, at))


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


def test_notify_texts_each_draft_once():
    m, t = Memory(":memory:"), FakeTwilio()
    aid = _draft(m)
    assert notify_pending(m, t, ME) == 1
    assert f"YES {aid}" in t.sent[0][1] and "prof@x.edu" in t.sent[0][1]
    assert notify_pending(m, t, ME) == 0


def test_yes_with_id_approves_and_confirms():
    m, t = Memory(":memory:"), FakeTwilio()
    aid = _draft(m)
    notify_pending(m, t, ME)
    t.reply(f"yes {aid}")
    assert handle_replies(m, t, ME) == 1
    assert m.get_action(aid)["status"] == "approved"
    assert t.sent[-1][1].startswith(f"Approved #{aid}")


def test_bare_yes_needs_exactly_one_open_draft():
    m, t = Memory(":memory:"), FakeTwilio()
    a, b = _draft(m, "A"), _draft(m, "B")
    notify_pending(m, t, ME)
    t.reply("yes")
    assert handle_replies(m, t, ME) == 0
    assert "Which one?" in t.sent[-1][1]
    assert m.get_action(a)["status"] == m.get_action(b)["status"] == "pending"


def test_replies_are_processed_once():
    m, t = Memory(":memory:"), FakeTwilio()
    aid = _draft(m)
    notify_pending(m, t, ME)
    t.reply(f"no {aid}")
    assert handle_replies(m, t, ME) == 1
    assert handle_replies(m, t, ME) == 0
    assert m.get_action(aid)["status"] == "rejected"


def test_ignores_strangers_and_texts_older_than_the_draft():
    m, t = Memory(":memory:"), FakeTwilio()
    t.reply("yes 1", at=datetime.now(timezone.utc) - timedelta(hours=1))
    aid = _draft(m)
    notify_pending(m, t, ME)
    t.reply(f"yes {aid}", sender=STRANGER)
    handle_replies(m, t, ME)
    assert m.get_action(aid)["status"] == "pending"


def test_cannot_approve_an_already_rejected_draft():
    m, t = Memory(":memory:"), FakeTwilio()
    aid = _draft(m)
    notify_pending(m, t, ME)
    t.reply(f"no {aid}")
    t.reply(f"yes {aid}", at=datetime.now(timezone.utc) + timedelta(seconds=5))
    assert handle_replies(m, t, ME) == 1
    assert m.get_action(aid)["status"] == "rejected"
    assert "can't be changed (rejected)" in t.sent[-1][1]
