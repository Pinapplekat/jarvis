-- M8: a node's events carry an id, so one that is resent after a dropped link lands in the outbox only once.
ALTER TABLE outbox ADD COLUMN dedupe_key TEXT;
CREATE UNIQUE INDEX outbox_dedupe ON outbox(dedupe_key);
