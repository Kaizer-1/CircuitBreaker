-- Where the outbox relay starts reading (Phase 4): the oldest minute bucket that may still hold an unpublished row.
-- One row. The relay moves it forward only past minutes it has read and found empty, so a cancel or an expiry
-- request is found however long it has waited (ADR-0017 note "No time limit for cancels and expiries").
CREATE TABLE IF NOT EXISTS outbox_cursor (
    name   text PRIMARY KEY,  -- 'relay'
    bucket bigint             -- minutes since 1970-01-01
);
