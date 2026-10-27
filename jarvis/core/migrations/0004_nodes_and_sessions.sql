-- M5: paired devices (only the key's SHA-256 is kept) and the live-view sessions the host tracks.
CREATE TABLE nodes (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen TEXT,
    revoked_at TEXT,
    capabilities_json TEXT NOT NULL DEFAULT '[]',  -- what it advertised at its last hello
    connected_at TEXT                              -- set while connected to a running host, so other commands can list it
);
CREATE UNIQUE INDEX nodes_active_name ON nodes(name) WHERE revoked_at IS NULL;  -- a revoked name can be paired again
CREATE TABLE sessions (
    id INTEGER PRIMARY KEY,
    app TEXT NOT NULL,
    name TEXT NOT NULL,
    node_id INTEGER REFERENCES nodes(id),         -- null when the app runs on the host
    node_conn INTEGER,                            -- which connection of that node holds the live resource
    state_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    last_look_at TEXT,
    stopped_at TEXT,
    stop_reason TEXT
);
CREATE INDEX sessions_open ON sessions(stopped_at);
