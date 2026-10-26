-- Baseline: the old JARVIS schema (schema only, no data), so a fresh database matches a copied one.
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY,
    category TEXT NOT NULL,          -- school | work | money | personal | preference
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    source TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(category, key)
);
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('business', 'personal', 'school')),
    status TEXT NOT NULL DEFAULT 'active',
    summary TEXT NOT NULL DEFAULT '',
    goals TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
, repo TEXT, brief TEXT, brief_at TEXT);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,              -- note | alert_sent | email_sent | sync | ...
    project_id INTEGER REFERENCES projects(id),
    content TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_actions (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,              -- send_email | call | ...
    payload TEXT NOT NULL,           -- JSON
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected', 'done', 'failed')),
    created_at TEXT NOT NULL,
    resolved_at TEXT
, notified_at TEXT);
CREATE TABLE IF NOT EXISTS processed_sms (
    sid TEXT PRIMARY KEY,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    level TEXT NOT NULL CHECK (level IN ('high', 'urgent')),
    message TEXT NOT NULL,
    dedupe_key TEXT UNIQUE,          -- same key never alerts twice
    created_at TEXT NOT NULL,
    texted_at TEXT,
    called_at TEXT
);
CREATE TABLE IF NOT EXISTS deadlines (
    key TEXT PRIMARY KEY,            -- canvas:<assignment id> overrides the feed's copy
    course TEXT NOT NULL,
    title TEXT NOT NULL,
    due TEXT NOT NULL,               -- ISO 8601 with offset
    url TEXT,
    source TEXT NOT NULL,            -- weekly | manual
    done INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_log (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY,
    due_at TEXT NOT NULL,            -- ISO 8601 with offset
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    cancelled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS seen_mail (
    key TEXT PRIMARY KEY,            -- account:message id
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    source TEXT NOT NULL,            -- screen | terminal | git
    app TEXT,
    title TEXT,
    project TEXT,                    -- repo name when it can be told
    text TEXT NOT NULL
, tag TEXT);
CREATE INDEX IF NOT EXISTS observations_at ON observations(at);
CREATE TABLE IF NOT EXISTS judgments (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    trigger TEXT NOT NULL,           -- periodic | stuck
    signals TEXT NOT NULL,           -- JSON list
    answers TEXT NOT NULL,           -- JSON {key: {value, confidence}}
    spoke INTEGER NOT NULL,
    blocked TEXT,                    -- rule that kept JARVIS quiet, if any
    message TEXT
);
CREATE TABLE IF NOT EXISTS journal (
    day TEXT PRIMARY KEY,            -- local date, YYYY-MM-DD
    entry TEXT NOT NULL,
    open_loops TEXT NOT NULL,        -- JSON list
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    goal TEXT NOT NULL,
    -- queued -> running <-> waiting (for Elijah's Run/Reject) -> done | failed | stopped
    status TEXT NOT NULL DEFAULT 'queued',
    steps TEXT NOT NULL DEFAULT '[]',  -- JSON: [{n, update, action, result, check}]
    message_id INTEGER,                -- the Telegram progress message
    waiting_on INTEGER,                -- pending_actions id while waiting
    summary TEXT,
    active_seconds REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
, session_id TEXT, folder TEXT, resume_note TEXT, blocked TEXT, runs INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS location_points (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,                -- first seen here
    last_seen TEXT NOT NULL,         -- still here as of (nearby updates extend the point)
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    accuracy REAL,                   -- metres, when Telegram gives it
    source TEXT NOT NULL,            -- static | live
    live_until TEXT                  -- when a live share ends (NULL: static, or until turned off)
);
CREATE INDEX IF NOT EXISTS location_points_at ON location_points(at);
CREATE TABLE IF NOT EXISTS places (
    id INTEGER PRIMARY KEY,
    name TEXT,                       -- 'school', 'work'...; NULL until Elijah labels it
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    asked_at TEXT,                   -- when JARVIS asked what this place is
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS place_visits (
    day TEXT NOT NULL,               -- local date
    place_id INTEGER NOT NULL,       -- 0 = "there was location data this day"
    first TEXT NOT NULL,             -- local HH:MM
    last TEXT NOT NULL,
    PRIMARY KEY (day, place_id)
);
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    source TEXT NOT NULL,            -- task | job | build
    ref INTEGER,                     -- the task / job / plan id
    kind TEXT NOT NULL,              -- done | failed | note | progress
    about TEXT NOT NULL,             -- what the work is, in a few words
    text TEXT NOT NULL,
    delivered_at TEXT                -- passed on to Elijah (progress: set at once, never sent)
, attachments TEXT);
CREATE TABLE IF NOT EXISTS watches (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,              -- location | email | screen | web | canvas | time | battery
    params TEXT NOT NULL,            -- JSON
    interval_min INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    recurring INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_checked TEXT,
    last_state TEXT,                 -- JSON: what the last check saw, for the cheap comparison
    fired_at TEXT,
    status TEXT NOT NULL DEFAULT 'active'  -- active | fired | expired | cancelled
);
CREATE TABLE IF NOT EXISTS parked_turns (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    deadline TEXT NOT NULL,          -- after this, he's told what's still pending
    channel TEXT,                    -- telegram | mac | mic | call
    message TEXT NOT NULL,
    state TEXT NOT NULL,             -- JSON: turn log, findings, what it waits on, active seconds
    consumed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS self_edits (
    id INTEGER PRIMARY KEY,
    origin TEXT NOT NULL CHECK (origin IN ('request', 'idea', 'prodmode')),  -- prodmode: a nightly fix (prodmode.py)
    title TEXT NOT NULL,
    request TEXT NOT NULL,           -- what to change, in plain words
    plan TEXT,                       -- JARVIS's plan, shown to Elijah for approval
    -- drafting -> proposed -> approved | declined; approved -> queued -> building -> ready
    -- -> deploying -> deployed (only when self-editing is enabled), or failed | discarded
    status TEXT NOT NULL,
    notified INTEGER NOT NULL DEFAULT 0,
    summary TEXT,                    -- what the build did / why it failed
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
