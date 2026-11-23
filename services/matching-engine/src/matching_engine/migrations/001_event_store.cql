-- Keyspace matching_engine (docs/03-architecture/02-data-ownership.md). Applied at start-up; idempotent.

-- The market event store: one partition per ticker per day, ordered by sequence.
CREATE TABLE IF NOT EXISTS market_events (
    ticker      text,
    day         date,
    sequence    bigint,
    event_id    text,
    event_type  text,
    occurred_at timestamp,
    payload     text,  -- the full JSON envelope
    PRIMARY KEY ((ticker, day), sequence)
) WITH CLUSTERING ORDER BY (sequence ASC);

-- Which day partitions exist per ticker, and the first sequence in each (rebuild and Q-14 paging).
CREATE TABLE IF NOT EXISTS ticker_days (
    ticker         text,
    day            date,
    first_sequence bigint,
    PRIMARY KEY (ticker, day)
) WITH CLUSTERING ORDER BY (day ASC);

-- Book snapshots: 002_snapshots_by_hour.cql (the first table, one partition per ticker, grew without
-- bound and is moved and dropped at start-up: adapters/cassandra_store.py).

-- Idempotency: the event_id of every orders.v1 command already processed.
CREATE TABLE IF NOT EXISTS processed_commands (
    command_id   text PRIMARY KEY,
    ticker       text,
    processed_at timestamp
);

-- The last sequence published to matching.v1, per ticker.
CREATE TABLE IF NOT EXISTS publish_cursor (
    ticker   text PRIMARY KEY,
    sequence bigint
);
