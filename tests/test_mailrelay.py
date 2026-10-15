from datetime import datetime

from jarvis.mailrelay import _parse_stamp, process_approved
from jarvis.memory import Memory


def _queue(m: Memory, subject: str) -> int:
    return m.queue_action("send_email", {
        "from": "me@x.edu", "to": ["a@x.edu"], "subject": subject, "body": "hi",
    })


def test_only_approved_emails_are_sent():
    m = Memory(":memory:")
    approved, pending, rejected = _queue(m, "A"), _queue(m, "B"), _queue(m, "C")
    m.resolve_action(approved, "approved")
    m.resolve_action(rejected, "rejected")
    calls = []
    assert process_approved(m, send=lambda *a: calls.append(a)) == (1, 0)
    assert calls == [("me@x.edu", ["a@x.edu"], "A", "hi")]
    assert m.get_action(approved)["status"] == "done"
    assert m.get_action(pending)["status"] == "pending"


def test_failed_send_is_marked_and_not_retried():
    m = Memory(":memory:")
    aid = _queue(m, "A")
    m.resolve_action(aid, "approved")

    def boom(*_):
        raise RuntimeError("Mail not running")

    assert process_approved(m, send=boom) == (0, 1)
    assert m.get_action(aid)["status"] == "failed"
    assert "Mail not running" in m.recent_events()[0]["content"]
    assert process_approved(m, send=boom) == (0, 0)


def test_parse_stamp():
    assert _parse_stamp("2026-10-5-3661") == datetime(2026, 10, 5, 1, 1, 1)
