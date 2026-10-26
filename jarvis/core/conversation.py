"""The conversation log: what Elijah said and what Falak answered (the chat_log table)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


def add(conn: sqlite3.Connection, role: str, text: str, now: datetime | None = None) -> int:
    at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")
    return conn.execute("INSERT INTO chat_log (at, role, text) VALUES (?,?,?)", (at, role, text)).lastrowid


def recent(conn: sqlite3.Connection, n: int, before_id: int | None = None) -> list[sqlite3.Row]:
    """The last `n` messages (oldest first), optionally only those before message `before_id`."""
    rows = conn.execute(
        "SELECT * FROM chat_log WHERE id < ? ORDER BY id DESC LIMIT ?",
        (before_id if before_id is not None else 2**62, n),
    ).fetchall()
    return rows[::-1]
