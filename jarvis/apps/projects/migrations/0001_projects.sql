CREATE TABLE IF NOT EXISTS projects_items (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    path TEXT,                          -- the repo folder, if it has one
    kind TEXT NOT NULL DEFAULT 'other', -- business | personal | school | other
    status TEXT NOT NULL DEFAULT 'unknown',   -- active | paused | stalled | done | unknown
    summary TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',     -- the old brief, then whatever Elijah has Falak add
    updated_at TEXT NOT NULL
);
-- Take over the old projects once, as reminders did; the legacy table is left alone. The old `repo` is the path,
-- the `brief` the notes (with the goals appended when there are any).
INSERT OR IGNORE INTO projects_items (id, name, path, kind, status, summary, notes, updated_at)
    SELECT id, name, repo, kind, status, summary,
           TRIM(COALESCE(brief, '') || CASE WHEN TRIM(COALESCE(goals, '')) != ''
                                            THEN CHAR(10) || CHAR(10) || 'Goals: ' || TRIM(goals) ELSE '' END),
           updated_at
    FROM projects;
