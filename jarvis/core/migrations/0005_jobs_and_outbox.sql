-- M6: what the scheduler knows about each job, a log of its runs, and the outbox every first-spoken message goes through.
CREATE TABLE jobs (
    app TEXT NOT NULL,
    name TEXT NOT NULL,
    next_run_at TEXT,             -- the earliest of its trigger, a wake and a retry (UTC)
    last_run_at TEXT,
    last_ok_at TEXT,
    last_status TEXT,             -- ok | failed | skipped | missed
    last_error TEXT,
    last_ms INTEGER,
    failures INTEGER NOT NULL DEFAULT 0,   -- in a row, counted only while online
    reported_at TEXT,             -- when Elijah was told it keeps failing; cleared on recovery
    PRIMARY KEY (app, name)
);
CREATE TABLE job_runs (
    id INTEGER PRIMARY KEY,
    app TEXT NOT NULL,
    name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ms INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,         -- ok | failed | skipped | missed
    reason TEXT NOT NULL,         -- every | at | wake | manual | catch-up
    summary TEXT,
    error TEXT
);
CREATE INDEX job_runs_started ON job_runs(started_at);
CREATE TABLE outbox (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    source TEXT NOT NULL,         -- "reminders.deliver", "job:school.nudge", "scheduler"
    created_at TEXT NOT NULL,
    not_before TEXT,              -- held until then (quiet hours)
    sent_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE INDEX outbox_unsent ON outbox(sent_at);
