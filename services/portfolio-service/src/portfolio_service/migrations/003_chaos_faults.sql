-- Phase 7, chaos hook A-21/A-22 ("Portfolio database degraded", chaos scenarios section 3.4). A row here makes
-- every reservation wait inside Postgres (pg_sleep). It lives in the database, not in a pod, so restarting,
-- rolling back or scaling portfolio-service cannot clear it: the cause is the data store. Only chaos-service sets
-- it (through A-21, with CHAOS_HOOKS_ENABLED=true); A-22 or a chaos reset removes it.
CREATE TABLE IF NOT EXISTS chaos_faults (
    fault      text PRIMARY KEY CHECK (fault = 'DB_DEGRADED'),
    delay_ms   integer NOT NULL CHECK (delay_ms BETWEEN 100 AND 5000),
    started_at timestamptz NOT NULL
);
