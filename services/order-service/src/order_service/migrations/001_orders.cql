-- Keyspace order_service (docs/03-architecture/02-data-ownership.md). Applied at start-up; idempotent.

-- Q-08: one order. The API writes cancel_requested; the matching.v1 consumer writes the status
-- columns. Cassandra merges writes per column, so neither overwrites the other.
CREATE TABLE IF NOT EXISTS orders_by_id (
    order_id               text PRIMARY KEY,
    trader_id              text,
    ticker                 text,
    side                   text,
    order_type             text,
    quantity               int,
    limit_price_paise      bigint,
    protection_price_paise bigint,
    reservation_id         text,
    status                 text,
    filled_quantity        int,
    filled_value_paise     bigint,
    cancel_requested       boolean,
    cancel_reason          text,
    rejection_reason       text,
    last_sequence          bigint,
    published              boolean,  -- its OrderPlaced reached orders.v1
    placed_at              timestamp,
    updated_at             timestamp
);

-- Q-07: a trader's orders, newest first.
CREATE TABLE IF NOT EXISTS orders_by_trader (
    trader_id text,
    placed_at timestamp,
    order_id  text,
    PRIMARY KEY (trader_id, placed_at, order_id)
) WITH CLUSTERING ORDER BY (placed_at DESC, order_id DESC);

-- Every event of an order (OrderPlaced, OrderRejected, OrderCancelRequested, OrderExpiryRequested).
CREATE TABLE IF NOT EXISTS order_events (
    order_id    text,
    occurred_at timestamp,
    event_id    text,
    event_type  text,
    payload     text,     -- the full JSON envelope
    published   boolean,
    PRIMARY KEY (order_id, occurred_at, event_id)
) WITH CLUSTERING ORDER BY (occurred_at ASC, event_id ASC);

-- The outbox: events not yet published, one partition per minute so the relay reads small partitions.
-- A row is deleted once published.
CREATE TABLE IF NOT EXISTS outbox (
    bucket      bigint,   -- minutes since 1970-01-01
    occurred_at timestamp,
    event_id    text,
    order_id    text,
    ticker      text,
    payload     text,
    PRIMARY KEY (bucket, occurred_at, event_id)
) WITH CLUSTERING ORDER BY (occurred_at ASC, event_id ASC);

-- Idempotency-Key of POST /v1/orders: kept 24 hours.
CREATE TABLE IF NOT EXISTS orders_by_idempotency_key (
    trader_id       text,
    idempotency_key text,
    order_id        text,
    PRIMARY KEY ((trader_id, idempotency_key))
) WITH default_time_to_live = 86400;

-- Idempotent consumption of matching.v1.
CREATE TABLE IF NOT EXISTS processed_events (
    event_id     text PRIMARY KEY,
    processed_at timestamp
);
