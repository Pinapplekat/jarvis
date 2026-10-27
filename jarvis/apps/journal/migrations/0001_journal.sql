CREATE TABLE IF NOT EXISTS journal_entries (
    day TEXT PRIMARY KEY,            -- local date, YYYY-MM-DD
    entry TEXT NOT NULL,
    open_loops TEXT NOT NULL,        -- JSON list
    created_at TEXT NOT NULL
);
-- Take over the old journal; the legacy table is left alone.
INSERT OR IGNORE INTO journal_entries (day, entry, open_loops, created_at)
    SELECT day, entry, open_loops, created_at FROM journal;
