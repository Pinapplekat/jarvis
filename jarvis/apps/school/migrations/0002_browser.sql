-- Announcements read through Falak's browser that look like they move a date. They are shown in the next check-in and
-- never change a deadline by themselves.
CREATE TABLE IF NOT EXISTS school_notices (
    id TEXT PRIMARY KEY,             -- canvas:<announcement id>
    course TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,              -- a short plain-text excerpt; someone else's words
    posted_at TEXT NOT NULL,
    first_seen_at TEXT NOT NULL      -- when the sync first stored it: what "the next check-in" counts from
);
-- Small facts the school app keeps for itself, like "told Elijah Canvas wants a sign-in".
CREATE TABLE IF NOT EXISTS school_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
