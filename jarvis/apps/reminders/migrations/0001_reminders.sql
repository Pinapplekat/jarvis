CREATE TABLE IF NOT EXISTS reminders_items (
    id INTEGER PRIMARY KEY,
    due_at TEXT NOT NULL,            -- ISO 8601 with offset
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending'   -- pending | sent | cancelled | imported (already past due when copied)
);
CREATE INDEX IF NOT EXISTS reminders_items_pending ON reminders_items(status, due_at);
-- Take over the old reminders; the legacy table is left alone. Ones already past due are not fired late.
INSERT OR IGNORE INTO reminders_items (id, due_at, text, created_at, sent_at, status)
    SELECT id, due_at, text, created_at, sent_at,
           CASE WHEN cancelled != 0 THEN 'cancelled'
                WHEN sent_at IS NOT NULL THEN 'sent'
                WHEN julianday(due_at) < julianday('now') THEN 'imported'
                ELSE 'pending' END
    FROM reminders;
