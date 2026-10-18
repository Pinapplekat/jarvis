from datetime import datetime

from jarvis.mailrelay import _parse_stamp, approve, email_risk, process_approved, queue_email
from jarvis.memory import Memory


def _queue(m: Memory, subject: str) -> int:
    return m.queue_action("send_email", {
        "from": "me@x.edu", "to": ["a@x.edu"], "subject": subject, "body": "hi",
    })


def test_only_approved_emails_are_sent():
    m = Memory(":memory:")
    approved, pending, rejected = _queue(m, "A"), _queue(m, "B"), _queue(m, "C")
    approve(m, approved)
    m.resolve_action(rejected, "rejected")
    calls = []
    assert process_approved(m, send=lambda *a: calls.append(a)) == (1, 0)
    assert calls == [("me@x.edu", ["a@x.edu"], "A", "hi")]
    assert m.get_action(approved)["status"] == "done"
    assert m.get_action(pending)["status"] == "pending"


def test_failed_send_is_marked_and_not_retried():
    m = Memory(":memory:")
    aid = _queue(m, "A")
    approve(m, aid)

    def boom(*_):
        raise RuntimeError("Mail not running")

    assert process_approved(m, send=boom) == (0, 1)
    assert m.get_action(aid)["status"] == "failed"
    assert "Mail not running" in m.recent_events()[0]["content"]
    assert process_approved(m, send=boom) == (0, 0)


def test_parse_stamp():
    assert _parse_stamp("2026-10-5-3661") == datetime(2026, 10, 5, 1, 1, 1)


def test_an_edit_after_approval_voids_it():
    import json
    m = Memory(":memory:")
    aid = _queue(m, "A")
    approve(m, aid)
    p = json.loads(m.get_action(aid)["payload"])
    m.db.execute("UPDATE pending_actions SET payload = ? WHERE id = ?", (json.dumps({**p, "body": "changed"}), aid))
    calls = []
    assert process_approved(m, send=lambda *a: calls.append(a)) == (0, 1)
    assert calls == [] and "changed after approval" in m.recent_events()[0]["content"]


def test_approved_without_confirming_isnt_sent():
    m = Memory(":memory:")
    aid = _queue(m, "A")
    m.resolve_action(aid, "approved")  # status alone, no recorded yes
    assert process_approved(m, send=lambda *a: None) == (0, 1)


def test_mail_only_to_himself_is_low_risk_and_goes_out_on_its_own(monkeypatch):
    from jarvis import config
    monkeypatch.setattr(config, "MAIL_ACCOUNT", "me@x.edu")
    assert email_risk({"from": "me@x.edu", "to": ["ME@x.edu"]}) == "low"
    assert email_risk({"from": "me@x.edu", "to": ["me@x.edu", "prof@x.edu"]}) == "high"
    assert email_risk({"from": "me@x.edu", "to": []}) == "high"
    m = Memory(":memory:")
    mine, _ = queue_email(m, {"from": "me@x.edu", "to": ["me@x.edu"], "subject": "note", "body": "x"})
    theirs, _ = queue_email(m, {"from": "me@x.edu", "to": ["prof@x.edu"], "subject": "hi", "body": "x"})
    assert m.get_action(mine)["status"] == "approved" and m.get_action(theirs)["status"] == "pending"
    assert m.unnotified_actions()[0]["id"] == theirs  # only the risky one is asked about
