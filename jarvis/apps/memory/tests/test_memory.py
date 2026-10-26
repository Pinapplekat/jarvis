import sqlite3

from jarvis.core import actions

CLI = actions.Origin("cli")


def do(reg, action, **args):
    return actions.run(reg, f"memory__{action}", args, CLI)


def rows(reg):
    return [tuple(r) for r in reg.conn.execute("SELECT * FROM memory_facts ORDER BY id")]


def test_remember_recall_forget(real_apps):
    do(real_apps, "remember", category="school", key="major", value="Computer Science")
    out = do(real_apps, "recall", query="COMPUTER")
    assert out.result.data["facts"][0]["key"] == "major"
    assert do(real_apps, "recall", query="major", category="work").result.data["facts"] == []
    assert isinstance(do(real_apps, "forget", category="school", key="major"), actions.Ran)
    assert do(real_apps, "recall", query="major").result.data["facts"] == []
    assert isinstance(do(real_apps, "forget", category="school", key="major"), actions.Failed)


def test_category_enum_and_limit(real_apps):
    assert isinstance(do(real_apps, "remember", category="hobby", key="k", value="v"), actions.Failed)
    for i in range(25):
        do(real_apps, "remember", category="work", key=f"k{i}", value="same")
    assert len(do(real_apps, "recall", query="same").result.data["facts"]) == 20


def test_undo_new_fact_removes_it(real_apps):
    do(real_apps, "remember", category="personal", key="t", value="x")
    assert isinstance(actions.undo(real_apps), actions.Ran)
    assert rows(real_apps) == []


def test_undo_overwrite_restores_exactly(real_apps):
    do(real_apps, "remember", category="personal", key="t", value="old")
    before = rows(real_apps)
    do(real_apps, "remember", category="personal", key="t", value="new")
    assert rows(real_apps) != before
    actions.undo(real_apps)
    assert rows(real_apps) == before


def test_undo_forget_restores_exactly(real_apps):
    do(real_apps, "remember", category="money", key="rent", value="1200")
    before = rows(real_apps)
    do(real_apps, "forget", category="money", key="rent")
    actions.undo(real_apps)
    assert rows(real_apps) == before


def test_migration_copies_legacy_facts_and_leaves_them(settings):
    from jarvis.core import db
    from jarvis.core.apps import load_apps

    conn = db.connect(settings)
    conn.execute("INSERT INTO facts (category, key, value, source, updated_at) VALUES "
                 "('school','major','CS','chat','2026-01-01')")
    conn.close()
    reg = load_apps(settings)
    try:
        assert rows(reg) == [(1, "school", "major", "CS", "chat", "2026-01-01")]
        assert reg.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 1
    finally:
        reg.close()
