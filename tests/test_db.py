import sqlite3

import pytest

from jarvis.core import db

BASELINE_TABLES = {
    "facts", "projects", "events", "pending_actions", "processed_sms", "alerts", "deadlines", "kv",
    "chat_log", "reminders", "seen_mail", "observations", "judgments", "journal", "tasks",
    "location_points", "places", "place_visits", "reports", "watches", "parked_turns", "self_edits",
}


def tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_fresh_db_has_baseline(settings):
    conn = db.connect(settings)
    assert BASELINE_TABLES <= tables(conn)
    assert db.applied_versions(conn) == [1, 2, 3, 4]
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_migrations_idempotent(settings):
    conn = db.connect(settings)
    assert db.migrate(conn) == []
    conn.close()
    assert db.applied_versions(db.connect(settings)) == [1, 2, 3, 4]


def test_second_migration_applies_once(settings, tmp_path):
    conn = db.connect(settings)
    extra = tmp_path / "migs"
    extra.mkdir()
    for f in db.MIGRATIONS_DIR.glob("*.sql"):
        (extra / f.name).write_text(f.read_text())
    (extra / "0005_extra.sql").write_text("CREATE TABLE extra (id INTEGER PRIMARY KEY);")
    assert db.migrate(conn, extra) == [5]
    assert db.migrate(conn, extra) == []
    assert db.applied_versions(conn) == [1, 2, 3, 4, 5]


def test_failed_migration_rolls_back(settings, tmp_path):
    conn = db.connect(settings)
    bad = tmp_path / "migs"
    bad.mkdir()
    (bad / "0005_bad.sql").write_text("CREATE TABLE half (id INTEGER); SELECT * FROM nope;")
    with pytest.raises(sqlite3.Error):
        db.migrate(conn, bad)
    assert "half" not in tables(conn)
    assert db.applied_versions(conn) == [1, 2, 3, 4]


@pytest.fixture
def source_db(tmp_path):
    path = tmp_path / "live.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    c.execute("INSERT INTO kv VALUES ('a', '1'), ('b', '2')")
    c.commit()
    c.close()
    return path


def test_import_legacy_db(settings, source_db):
    counts = db.import_legacy_db(settings, source_db)
    assert counts["kv"] == 2
    assert counts["facts"] == 0  # created by the baseline migration
    assert db.applied_versions(db.connect(settings)) == [1, 2, 3, 4]


def test_import_refuses_then_overwrites(settings, source_db):
    db.import_legacy_db(settings, source_db)
    with pytest.raises(FileExistsError):
        db.import_legacy_db(settings, source_db)
    c = sqlite3.connect(source_db)
    c.execute("INSERT INTO kv VALUES ('c', '3')")
    c.commit()
    c.close()
    assert db.import_legacy_db(settings, source_db, overwrite=True)["kv"] == 3
