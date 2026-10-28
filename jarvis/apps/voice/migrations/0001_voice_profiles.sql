-- M9: how Falak sounds. The newest row is the current voice; older rows are the history (undo adds a row, it never
-- deletes one). A profile belongs to the engine it was made for.
CREATE TABLE IF NOT EXISTS voice_profiles (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    engine TEXT NOT NULL,
    profile_json TEXT NOT NULL,
    source TEXT NOT NULL          -- "set_voice" or "undo"
);
