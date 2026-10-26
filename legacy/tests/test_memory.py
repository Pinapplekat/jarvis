import json

from jarvis.memory import Memory


def test_facts_upsert_and_forget():
    m = Memory(":memory:")
    m.remember("school", "major", "CS")
    m.remember("school", "major", "Computer Engineering")
    assert [f["value"] for f in m.facts("school")] == ["Computer Engineering"]
    assert m.forget("school", "major")
    assert not m.forget("school", "major")


def test_projects_insert_then_update_keeps_fields():
    m = Memory(":memory:")
    pid = m.upsert_project("Shop", "business", summary="Online store", goals="First 10 sales")
    assert m.upsert_project("Shop", "business", status="paused") == pid
    (p,) = m.projects()
    assert (p["status"], p["summary"], p["goals"]) == ("paused", "Online store", "First 10 sales")


def test_pending_action_lifecycle():
    m = Memory(":memory:")
    aid = m.queue_action("send_email", {"to": ["prof@school.edu"], "subject": "Extension"})
    (a,) = m.pending_actions()
    assert json.loads(a["payload"])["subject"] == "Extension"
    m.resolve_action(aid, "approved")
    assert m.pending_actions() == []


def test_context_snapshot_includes_everything():
    m = Memory(":memory:")
    m.remember("money", "rent_due", "1st of month")
    pid = m.upsert_project("JARVIS", "personal", summary="Assistant")
    m.log("note", "Started scaffolding", pid)
    snap = m.context_snapshot()
    assert "rent_due" in snap and "JARVIS" in snap and "Started scaffolding" in snap
