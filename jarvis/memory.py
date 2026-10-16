import json
import sqlite3
from datetime import datetime, timedelta, timezone
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

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    level TEXT NOT NULL CHECK (level IN ('high', 'urgent')),
    message TEXT NOT NULL,
    dedupe_key TEXT UNIQUE,          -- same key never alerts twice
    created_at TEXT NOT NULL,
    texted_at TEXT,
    called_at TEXT
);

CREATE TABLE IF NOT EXISTS deadlines (
    key TEXT PRIMARY KEY,            -- canvas:<assignment id> overrides the feed's copy
    course TEXT NOT NULL,
    title TEXT NOT NULL,
    due TEXT NOT NULL,               -- ISO 8601 with offset
    url TEXT,
    source TEXT NOT NULL,            -- weekly | manual
    done INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_log (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    text TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY,
    due_at TEXT NOT NULL,            -- ISO 8601 with offset
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    cancelled INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    goal TEXT NOT NULL,
    -- queued -> running <-> waiting (for Elijah's Run/Reject) -> done | failed | stopped
    status TEXT NOT NULL DEFAULT 'queued',
    steps TEXT NOT NULL DEFAULT '[]',  -- JSON: [{n, update, action, result, check}]
    message_id INTEGER,                -- the Telegram progress message
    waiting_on INTEGER,                -- pending_actions id while waiting
    summary TEXT,
    active_seconds REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS self_edits (
    id INTEGER PRIMARY KEY,
    origin TEXT NOT NULL CHECK (origin IN ('request', 'idea')),
    title TEXT NOT NULL,
    request TEXT NOT NULL,           -- what to change, in plain words
    plan TEXT,                       -- JARVIS's plan, shown to Elijah for approval
    -- drafting -> proposed -> approved | declined; approved -> queued -> building -> ready
    -- -> deploying -> deployed (only when self-editing is enabled), or failed | discarded
    status TEXT NOT NULL,
    notified INTEGER NOT NULL DEFAULT 0,
    summary TEXT,                    -- what the build did / why it failed
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    source TEXT NOT NULL,            -- screen | terminal | git
    app TEXT,
    title TEXT,
    project TEXT,                    -- repo name when it can be told
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS observations_at ON observations(at);

CREATE TABLE IF NOT EXISTS judgments (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    trigger TEXT NOT NULL,           -- periodic | stuck
    signals TEXT NOT NULL,           -- JSON list
    answers TEXT NOT NULL,           -- JSON {key: {value, confidence}}
    spoke INTEGER NOT NULL,
    blocked TEXT,                    -- rule that kept JARVIS quiet, if any
    message TEXT
);

CREATE TABLE IF NOT EXISTS journal (
    day TEXT PRIMARY KEY,            -- local date, YYYY-MM-DD
    entry TEXT NOT NULL,
    open_loops TEXT NOT NULL,        -- JSON list
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seen_mail (
    key TEXT PRIMARY KEY,            -- account:message id
    at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

MIGRATIONS = [
    ("pending_actions", "notified_at", "ALTER TABLE pending_actions ADD COLUMN notified_at TEXT"),
    ("projects", "repo", "ALTER TABLE projects ADD COLUMN repo TEXT"),
    ("projects", "brief", "ALTER TABLE projects ADD COLUMN brief TEXT"),
    ("projects", "brief_at", "ALTER TABLE projects ADD COLUMN brief_at TEXT"),
    ("observations", "tag", "ALTER TABLE observations ADD COLUMN tag TEXT"),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class Memory:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        for table, column, sql in MIGRATIONS:
            cols = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self.db.execute(sql)
        self.db.commit()

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

    def link_repo(self, name: str, repo: str) -> None:
        self.db.execute("UPDATE projects SET repo = ? WHERE name = ?", (repo, name))
        self.db.commit()

    def set_brief(self, name: str, brief: str) -> None:
        self.db.execute("UPDATE projects SET brief = ?, brief_at = ? WHERE name = ?", (brief, _now(), name))
        self.db.commit()

    def project(self, name: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM projects WHERE name = ? COLLATE NOCASE", (name,)).fetchone()

    def save_journal(self, day: str, entry: str, open_loops: list[str], at: datetime | None = None) -> None:
        self.db.execute(
            """INSERT INTO journal (day, entry, open_loops, created_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(day) DO UPDATE SET entry = excluded.entry, open_loops = excluded.open_loops,
                 created_at = excluded.created_at""",
            (day, entry, json.dumps(open_loops), _iso(at) if at else _now()),
        )
        self.db.commit()

    def journal(self, day: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM journal WHERE day = ?", (day,)).fetchone()

    def recent_journal(self, limit: int = 3) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM journal ORDER BY day DESC LIMIT ?", (limit,)).fetchall()

    def chat_between(self, start: datetime, end: datetime) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM chat_log WHERE at >= ? AND at < ? ORDER BY id", (_iso(start), _iso(end))
        ).fetchall()

    def events_between(self, start: datetime, end: datetime) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM events WHERE at >= ? AND at < ? ORDER BY id", (_iso(start), _iso(end))
        ).fetchall()

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

    def unnotified_actions(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM pending_actions WHERE status = 'pending' AND notified_at IS NULL ORDER BY id"
        ).fetchall()

    def mark_notified(self, action_id: int) -> None:
        self.db.execute(
            "UPDATE pending_actions SET notified_at = ? WHERE id = ?", (_now(), action_id)
        )
        self.db.commit()

    def add_reminder(self, due_at: datetime, text: str) -> int:
        cur = self.db.execute(
            "INSERT INTO reminders (due_at, text, created_at) VALUES (?, ?, ?)",
            (due_at.astimezone(timezone.utc).isoformat(timespec="seconds"), text, _now()),
        )
        self.db.commit()
        return cur.lastrowid

    def upcoming_reminders(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM reminders WHERE sent_at IS NULL AND cancelled = 0 ORDER BY due_at"
        ).fetchall()

    def cancel_reminder(self, reminder_id: int) -> bool:
        cur = self.db.execute(
            "UPDATE reminders SET cancelled = 1 WHERE id = ? AND sent_at IS NULL AND cancelled = 0",
            (reminder_id,),
        )
        self.db.commit()
        return cur.rowcount > 0

    def claim_due_reminders(self, now: datetime) -> list[sqlite3.Row]:
        """Mark due reminders as sent and return them. Safe if listener and tick both call it:
        each reminder is claimed by exactly one caller."""
        cutoff = now.astimezone(timezone.utc).isoformat(timespec="seconds")
        due = self.db.execute(
            "SELECT * FROM reminders WHERE sent_at IS NULL AND cancelled = 0 AND due_at <= ? ORDER BY due_at",
            (cutoff,),
        ).fetchall()
        claimed = []
        for r in due:
            cur = self.db.execute(
                "UPDATE reminders SET sent_at = ? WHERE id = ? AND sent_at IS NULL", (_now(), r["id"])
            )
            if cur.rowcount:
                claimed.append(r)
        self.db.commit()
        return claimed

    def add_self_edit(self, origin: str, title: str, request: str) -> int:
        """A new improvement starts as 'drafting': JARVIS writes a plan before anything else."""
        cur = self.db.execute(
            """INSERT INTO self_edits (origin, title, request, status, created_at, updated_at)
               VALUES (?, ?, ?, 'drafting', ?, ?)""",
            (origin, title, request, _now(), _now()),
        )
        self.db.commit()
        return cur.lastrowid

    def get_self_edit(self, edit_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM self_edits WHERE id = ?", (edit_id,)).fetchone()

    def self_edits(self, *statuses: str) -> list[sqlite3.Row]:
        if not statuses:
            return self.db.execute("SELECT * FROM self_edits ORDER BY id").fetchall()
        marks = ",".join("?" * len(statuses))
        return self.db.execute(
            f"SELECT * FROM self_edits WHERE status IN ({marks}) ORDER BY id", statuses
        ).fetchall()

    def set_self_edit(self, edit_id: int, expect: tuple[str, ...] | None = None, **fields) -> bool:
        """Update fields; with `expect`, only if the current status is one of those (so two
        button taps or processes can't both act on the same job)."""
        allowed = {"status", "notified", "summary", "plan", "request", "title"}
        if set(fields) - allowed:
            raise ValueError(f"bad fields {set(fields) - allowed}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        sql = f"UPDATE self_edits SET {sets}, updated_at = ? WHERE id = ?"
        params = [*fields.values(), _now(), edit_id]
        if expect:
            sql += f" AND status IN ({','.join('?' * len(expect))})"
            params += list(expect)
        cur = self.db.execute(sql, params)
        self.db.commit()
        return cur.rowcount > 0

    def observe(self, source: str, text: str, app: str | None = None, title: str | None = None,
                project: str | None = None, at: datetime | None = None, tag: str | None = None) -> None:
        stamp = (at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")
        self.db.execute(
            "INSERT INTO observations (at, source, app, title, project, text, tag) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (stamp, source, app, title, project, text, tag),
        )
        self.db.commit()

    def observations(self, since: datetime, source: str | None = None) -> list[sqlite3.Row]:
        sql, params = "SELECT * FROM observations WHERE at >= ?", [
            since.astimezone(timezone.utc).isoformat(timespec="seconds")]
        if source:
            sql += " AND source = ?"
            params.append(source)
        return self.db.execute(sql + " ORDER BY id", params).fetchall()

    def prune_observations(self, older_than: timedelta) -> int:
        cutoff = (datetime.now(timezone.utc) - older_than).isoformat(timespec="seconds")
        cur = self.db.execute("DELETE FROM observations WHERE at < ?", (cutoff,))
        self.db.commit()
        return cur.rowcount

    def add_judgment(self, trigger: str, signals: list[str], answers: dict, spoke: bool,
                     blocked: str | None = None, message: str | None = None,
                     at: datetime | None = None) -> None:
        stamp = (at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")
        self.db.execute(
            """INSERT INTO judgments (at, trigger, signals, answers, spoke, blocked, message)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (stamp, trigger, json.dumps(signals), json.dumps(answers), int(spoke), blocked, message),
        )
        self.db.commit()

    def recent_judgments(self, spoke_only: bool = False, limit: int = 10) -> list[sqlite3.Row]:
        sql = "SELECT * FROM judgments" + (" WHERE spoke = 1" if spoke_only else "")
        return self.db.execute(sql + " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def judgments_spoken_since(self, since: datetime) -> int:
        cutoff = since.astimezone(timezone.utc).isoformat(timespec="seconds")
        return self.db.execute(
            "SELECT COUNT(*) FROM judgments WHERE spoke = 1 AND at >= ?", (cutoff,)
        ).fetchone()[0]

    def mail_seen(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM seen_mail WHERE key = ?", (key,)).fetchone() is not None

    def mark_mail_seen(self, key: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO seen_mail (key, at) VALUES (?, ?)", (key, _now()))
        self.db.commit()

    def add_chat(self, role: str, text: str) -> None:
        self.db.execute("INSERT INTO chat_log (at, role, text) VALUES (?, ?, ?)", (_now(), role, text))
        self.db.commit()

    def chat_history(self, limit: int) -> list[sqlite3.Row]:
        """The last `limit` chat messages, oldest first."""
        rows = self.db.execute("SELECT * FROM chat_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return rows[::-1]

    def get_kv(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_kv(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.db.commit()

    def raise_alert(self, level: str, message: str, dedupe_key: str | None = None) -> int | None:
        """Record an alert to deliver. None if an alert with this key already exists."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO alerts (level, message, dedupe_key, created_at) VALUES (?, ?, ?, ?)",
            (level, message, dedupe_key, _now()),
        )
        self.db.commit()
        return cur.lastrowid if cur.rowcount else None

    def undelivered_alerts(self, max_age: timedelta = timedelta(hours=6)) -> list[sqlite3.Row]:
        """Alerts older than max_age are stale (e.g. raised while SMS was down) and dropped."""
        cutoff = (datetime.now(timezone.utc) - max_age).isoformat(timespec="seconds")
        return self.db.execute(
            """SELECT * FROM alerts
               WHERE (texted_at IS NULL OR (level = 'urgent' AND called_at IS NULL))
                 AND created_at >= ?
               ORDER BY id""",
            (cutoff,),
        ).fetchall()

    def mark_alert(self, alert_id: int, field: str) -> None:
        if field not in ("texted_at", "called_at"):
            raise ValueError(field)
        self.db.execute(f"UPDATE alerts SET {field} = ? WHERE id = ?", (_now(), alert_id))
        self.db.commit()

    def upsert_deadline(self, key: str, course: str, title: str, due: datetime,
                        source: str, url: str | None = None, done: bool | None = None) -> None:
        """Insert or update. done=None keeps the existing done flag (a re-import won't un-finish work)."""
        self.db.execute(
            """INSERT INTO deadlines (key, course, title, due, url, source, done, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                 course = excluded.course, title = excluded.title, due = excluded.due,
                 url = COALESCE(excluded.url, deadlines.url), source = excluded.source,
                 done = CASE WHEN ? IS NULL THEN deadlines.done ELSE excluded.done END,
                 updated_at = excluded.updated_at""",
            (key, course, title, due.isoformat(), url, source, int(bool(done)), _now(), done),
        )
        self.db.commit()

    def set_deadline_done(self, key: str, done: bool = True) -> bool:
        cur = self.db.execute(
            "UPDATE deadlines SET done = ?, updated_at = ? WHERE key = ?", (int(done), _now(), key)
        )
        self.db.commit()
        return cur.rowcount > 0

    def deadlines(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM deadlines ORDER BY due").fetchall()

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
