"""SQLite connection and numbered migrations."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from jarvis.core.settings import Settings

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
LIVE_DB = Path("~/Library/Application Support/JARVIS/jarvis.db").expanduser()


def db_path(settings: Settings) -> Path:
    return settings.data_dir / "falak.db"


def _open(path: Path) -> sqlite3.Connection:
    # isolation_level=None: we manage transactions ourselves so a migration is atomic.
    # check_same_thread=False: actions run in worker threads so a slow device never blocks the event loop;
    # sqlite3 serializes access to one connection, and each statement here is its own transaction.
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")  # job threads and the main connection write the same file
    return conn


def connect_extra(settings: Settings) -> sqlite3.Connection:
    """Another connection to the same (already migrated) database, for one thread of its own."""
    return _open(db_path(settings))


def applied_versions(conn: sqlite3.Connection) -> list[int]:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if not exists:
        return []
    return [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]


def migrate(conn: sqlite3.Connection, migrations_dir: Path = MIGRATIONS_DIR, app: str | None = None) -> list[int]:
    """Apply pending migrations in order, each in its own transaction. Returns the versions applied.

    With `app`, applies that app's migrations and records them in app_migrations instead.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT)"
    )
    if app is None:
        done = set(applied_versions(conn))
    else:
        done = {r[0] for r in conn.execute("SELECT version FROM app_migrations WHERE app = ?", (app,))}
    ran: list[int] = []
    for path in sorted(migrations_dir.glob("*.sql")):
        version = int(path.name.split("_", 1)[0])
        if version in done:
            continue
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if app is None:
            record = f"INSERT INTO schema_migrations (version, applied_at) VALUES ({version}, '{stamp}');"
        else:  # the app name is validated by the manifest loader, so it is safe to inline
            record = f"INSERT INTO app_migrations (app, version, applied_at) VALUES ('{app}', {version}, '{stamp}');"
        script = f"BEGIN;\n{path.read_text()}\n{record}\nCOMMIT;"
        try:
            conn.executescript(script)
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        ran.append(version)
    return ran


def connect(settings: Settings) -> sqlite3.Connection:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = _open(db_path(settings))
    migrate(conn)
    return conn


def import_legacy_db(settings: Settings, source: Path = LIVE_DB, overwrite: bool = False) -> dict[str, int]:
    """Copy the live JARVIS database into falak.db (source opened read-only), migrate, return row counts."""
    target = db_path(settings)
    if target.exists() and not overwrite:
        raise FileExistsError(f"{target} already exists; pass overwrite=True to replace it")
    source = Path(source)
    if not source.exists():
        raise FileNotFoundError(f"source database not found: {source}")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        Path(str(target) + suffix).unlink(missing_ok=True)
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dest = sqlite3.connect(target)
        try:
            src.backup(dest)
        finally:
            dest.close()
    finally:
        src.close()
    conn = connect(settings)
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    finally:
        conn.close()
