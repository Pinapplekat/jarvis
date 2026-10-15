import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY,
    category TEXT NOT NULL,          -- school | work | money | personal | preference
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    source TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(category, key)
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('business', 'personal', 'school')),
    status TEXT NOT NULL DEFAULT 'active',
    summary TEXT NOT NULL DEFAULT '',
    goals TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,              -- note | alert_sent | email_sent | sync | ...
    project_id INTEGER REFERENCES projects(id),
    content TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_actions (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,              -- send_email | call | ...
    payload TEXT NOT NULL,           -- JSON
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected', 'done', 'failed')),
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Memory:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)

    def remember(self, category: str, key: str, value: str, source: str | None = None) -> None:
        self.db.execute(
            """INSERT INTO facts (category, key, value, source, updated_at) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(category, key) DO UPDATE SET
                 value = excluded.value, source = excluded.source, updated_at = excluded.updated_at""",
            (category, key, value, source, _now()),
        )
        self.db.commit()

    def forget(self, category: str, key: str) -> bool:
        cur = self.db.execute("DELETE FROM facts WHERE category = ? AND key = ?", (category, key))
        self.db.commit()
        return cur.rowcount > 0

    def facts(self, category: str | None = None) -> list[sqlite3.Row]:
        if category:
            return self.db.execute(
                "SELECT * FROM facts WHERE category = ? ORDER BY key", (category,)
            ).fetchall()
        return self.db.execute("SELECT * FROM facts ORDER BY category, key").fetchall()

    def upsert_project(self, name: str, kind: str, **fields: str) -> int:
        allowed = {"status", "summary", "goals"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown project fields: {unknown}")
        row = self.db.execute("SELECT id FROM projects WHERE name = ?", (name,)).fetchone()
        if row:
            sets = ", ".join(f"{k} = ?" for k in fields)
            self.db.execute(
                f"UPDATE projects SET kind = ?, {sets + ', ' if sets else ''}updated_at = ? WHERE id = ?",
                (kind, *fields.values(), _now(), row["id"]),
            )
            project_id = row["id"]
        else:
            cols = ", ".join(["name", "kind", *fields, "updated_at"])
            marks = ", ".join("?" * (len(fields) + 3))
            cur = self.db.execute(
                f"INSERT INTO projects ({cols}) VALUES ({marks})",
                (name, kind, *fields.values(), _now()),
            )
            project_id = cur.lastrowid
        self.db.commit()
        return project_id

    def projects(self, kind: str | None = None) -> list[sqlite3.Row]:
        if kind:
            return self.db.execute(
                "SELECT * FROM projects WHERE kind = ? ORDER BY name", (kind,)
            ).fetchall()
        return self.db.execute("SELECT * FROM projects ORDER BY kind, name").fetchall()

    def log(self, kind: str, content: str, project_id: int | None = None) -> None:
        self.db.execute(
            "INSERT INTO events (at, kind, project_id, content) VALUES (?, ?, ?, ?)",
            (_now(), kind, project_id, content),
        )
        self.db.commit()

    def recent_events(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def queue_action(self, kind: str, payload: dict) -> int:
        cur = self.db.execute(
            "INSERT INTO pending_actions (kind, payload, created_at) VALUES (?, ?, ?)",
            (kind, json.dumps(payload), _now()),
        )
        self.db.commit()
        return cur.lastrowid

    def pending_actions(self, status: str = "pending", kind: str | None = None) -> list[sqlite3.Row]:
        sql, params = "SELECT * FROM pending_actions WHERE status = ?", [status]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        return self.db.execute(sql + " ORDER BY id", params).fetchall()

    def get_action(self, action_id: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM pending_actions WHERE id = ?", (action_id,)
        ).fetchone()

    def resolve_action(self, action_id: int, status: str) -> None:
        self.db.execute(
            "UPDATE pending_actions SET status = ?, resolved_at = ? WHERE id = ?",
            (status, _now(), action_id),
        )
        self.db.commit()

    def context_snapshot(self) -> str:
        """Plain-text dump of what JARVIS knows, for feeding to the model."""
        lines = ["# Facts"]
        lines += [f"- [{f['category']}] {f['key']}: {f['value']}" for f in self.facts()]
        lines.append("\n# Projects")
        lines += [
            f"- {p['name']} ({p['kind']}, {p['status']}): {p['summary']}"
            + (f" | goals: {p['goals']}" if p["goals"] else "")
            for p in self.projects()
        ]
        lines.append("\n# Recent events")
        lines += [f"- {e['at']} {e['kind']}: {e['content']}" for e in self.recent_events()]
        return "\n".join(lines)
