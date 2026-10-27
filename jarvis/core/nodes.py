"""Pairing: which devices may connect. Only the SHA-256 of a node's key is ever stored."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3

from jarvis.core.timeutil import stamp

NAME = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")


class PairingError(Exception):
    pass


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def pair(conn: sqlite3.Connection, name: str) -> str:
    """Register a node and return its key. This is the only time the key exists in the clear."""
    if not NAME.match(name):
        raise PairingError("A node name is lowercase letters, digits, '-' or '_', starting with a letter.")
    if conn.execute("SELECT 1 FROM nodes WHERE name = ? AND revoked_at IS NULL", (name,)).fetchone():
        raise PairingError(f"There is already a node called {name}. Revoke it first to pair it again.")
    key = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO nodes (name, key_hash, created_at) VALUES (?,?,?)", (name, hash_key(key), stamp()))
    return key


def revoke(conn: sqlite3.Connection, name: str) -> None:
    changed = conn.execute("UPDATE nodes SET revoked_at = ?, connected_at = NULL WHERE name = ? AND revoked_at IS NULL",
                           (stamp(), name)).rowcount
    if not changed:
        raise PairingError(f"There is no active node called {name}.")


def list_nodes(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM nodes ORDER BY id").fetchall()
    return [{**dict(r), "capabilities": json.loads(r["capabilities_json"])} for r in rows]
