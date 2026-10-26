-- M3: what an action changed (so undo can refuse out-of-order), and one row per conversation turn.
ALTER TABLE action_log ADD COLUMN subject TEXT;
CREATE TABLE turns (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    channel TEXT NOT NULL,
    ms INTEGER NOT NULL,
    model TEXT NOT NULL,
    num_turns INTEGER NOT NULL DEFAULT 0,
    tools_json TEXT NOT NULL DEFAULT '[]',
    cost_usd REAL,
    error TEXT
);
