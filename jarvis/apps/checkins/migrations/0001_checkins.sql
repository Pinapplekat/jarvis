CREATE TABLE IF NOT EXISTS checkins_sent (
    day TEXT NOT NULL,               -- local date
    kind TEXT NOT NULL,              -- morning | evening
    sent_at TEXT NOT NULL,
    PRIMARY KEY (day, kind)
);
