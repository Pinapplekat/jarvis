-- The app contract: per-app migration bookkeeping, pending asks and the action log.
CREATE TABLE app_migrations (
    app TEXT NOT NULL,
    version INTEGER NOT NULL,
    applied_at TEXT NOT NULL,
    PRIMARY KEY (app, version)
);
CREATE TABLE asks (
    id INTEGER PRIMARY KEY,
    tool TEXT NOT NULL,
    args_json TEXT NOT NULL,
    question TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'declined', 'expired')),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    origin_json TEXT NOT NULL
);
CREATE TABLE action_log (
    id INTEGER PRIMARY KEY,
    time TEXT NOT NULL,
    tool TEXT NOT NULL,
    effect TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ran', 'failed', 'asked', 'declined')),
    args TEXT NOT NULL,         -- "(not logged)" when the action sets log_args = false
    result TEXT NOT NULL,       -- at most 500 characters; attachments are never logged
    undo_json TEXT,
    undone_at TEXT
);
