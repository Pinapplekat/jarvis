-- When each web search ran, for the daily cap. The query itself isn't kept.
CREATE TABLE IF NOT EXISTS web_searches (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL                 -- ISO 8601, UTC
);
