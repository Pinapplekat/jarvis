CREATE TABLE IF NOT EXISTS memory_facts (
    id INTEGER PRIMARY KEY,
    category TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    source TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(category, key)
);
-- Take over the old facts; the legacy table (created by the core baseline) is left alone.
INSERT OR IGNORE INTO memory_facts (id, category, key, value, source, updated_at)
    SELECT id, category, key, value, source, updated_at FROM facts;
