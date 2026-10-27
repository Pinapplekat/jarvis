CREATE TABLE IF NOT EXISTS school_deadlines (
    key TEXT PRIMARY KEY,            -- canvas:<assignment id>, module:<course>:<id>, feed:<uid> or a hand-made one
    course TEXT NOT NULL,
    title TEXT NOT NULL,
    due TEXT NOT NULL,               -- ISO 8601 with offset
    url TEXT,
    status TEXT NOT NULL DEFAULT 'unknown',   -- what Canvas says: submitted | graded | unsubmitted | missing | unknown
    trackable INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT 'due',         -- due | event
    hand_done INTEGER NOT NULL DEFAULT 0,     -- marked done by Elijah
    source TEXT NOT NULL,                     -- canvas | manual
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS school_nudges (
    key TEXT PRIMARY KEY,            -- <deadline key>:24h or :3h
    sent_at TEXT NOT NULL
);
-- Take over the old deadlines (kept by hand or by /weekly); the legacy table is left alone.
INSERT OR IGNORE INTO school_deadlines (key, course, title, due, url, status, trackable, kind, hand_done, source, updated_at)
    SELECT key, course, title, due, url, 'unknown', 0, 'due', done, 'manual', updated_at FROM deadlines;
