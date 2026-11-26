-- Keyspace sre_agent (docs/03-architecture/02-data-ownership.md). Applied at start-up; idempotent.

-- Q-18: one incident, its remediations and timeline as one JSON document (one read).
CREATE TABLE IF NOT EXISTS incidents (
    incident_id text PRIMARY KEY,
    opened_at   timestamp,
    doc         text
);

-- Q-17: incidents newest first, one partition per day.
CREATE TABLE IF NOT EXISTS incidents_by_day (
    day         date,
    opened_at   timestamp,
    incident_id text,
    PRIMARY KEY ((day), opened_at, incident_id)
) WITH CLUSTERING ORDER BY (opened_at DESC, incident_id DESC);

-- Q-20: the approval toggle and the pause switch, per environment.
CREATE TABLE IF NOT EXISTS agent_settings (
    environment       text PRIMARY KEY,
    approval_required boolean,
    paused            boolean,
    updated_at        timestamp,
    updated_by        text
);

-- The guardrails' memory: cooldown per service (G5) and the hourly budget (G6) survive a restart.
CREATE TABLE IF NOT EXISTS action_log (
    service     text,
    executed_at timestamp,
    incident_id text,
    action      text,
    PRIMARY KEY ((service), executed_at, incident_id)
) WITH CLUSTERING ORDER BY (executed_at DESC, incident_id ASC);

-- INC-nnnn numbers: the last one given out (a lightweight transaction moves it on).
CREATE TABLE IF NOT EXISTS incident_counter (
    name text PRIMARY KEY,
    last int
);
