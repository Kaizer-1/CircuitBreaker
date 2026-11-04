-- Database portfolio (docs/03-architecture/02-data-ownership.md). Applied once at start-up.
-- The CHECK constraints are the "no margin, no short selling" invariants, enforced by Postgres itself.

CREATE TABLE accounts (
    trader_id            uuid PRIMARY KEY,
    nickname             text NOT NULL UNIQUE,
    kind                 text NOT NULL CHECK (kind IN ('HUMAN', 'BOT', 'LOAD')),
    cash_paise           bigint NOT NULL,
    reserved_cash_paise  bigint NOT NULL DEFAULT 0,
    starting_value_paise bigint NOT NULL,
    created_at           timestamptz NOT NULL,
    CHECK (cash_paise >= reserved_cash_paise AND reserved_cash_paise >= 0)
);

CREATE TABLE holdings (
    trader_id         uuid NOT NULL REFERENCES accounts,
    ticker            text NOT NULL,
    quantity          bigint NOT NULL DEFAULT 0,
    reserved_quantity bigint NOT NULL DEFAULT 0,
    avg_cost_paise    bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (trader_id, ticker),
    CHECK (quantity >= reserved_quantity AND reserved_quantity >= 0)
);

CREATE TABLE reservations (
    reservation_id         uuid PRIMARY KEY,
    order_id               uuid NOT NULL UNIQUE,
    trader_id              uuid NOT NULL REFERENCES accounts,
    ticker                 text NOT NULL,
    side                   text NOT NULL CHECK (side IN ('BUY', 'SELL')),
    order_type             text NOT NULL CHECK (order_type IN ('LIMIT', 'MARKET')),
    quantity               integer NOT NULL CHECK (quantity > 0),
    limit_price_paise      bigint,
    protection_price_paise bigint,
    reserved_cash_paise    bigint NOT NULL,
    reserved_quantity      bigint NOT NULL,
    remaining_cash_paise   bigint NOT NULL CHECK (remaining_cash_paise >= 0),
    remaining_quantity     bigint NOT NULL CHECK (remaining_quantity >= 0),
    status                 text NOT NULL CHECK (status IN ('ACTIVE', 'CONSUMED', 'RELEASED')),
    confirmed              boolean NOT NULL DEFAULT false,  -- OrderAccepted seen
    expiry_requested_at    timestamptz,                     -- ADR-0017 sweeper asked order-service
    created_at             timestamptz NOT NULL
);
-- The sweeper's query: unconfirmed, still active, not yet asked about.
CREATE INDEX reservations_orphan_candidates ON reservations (created_at)
    WHERE status = 'ACTIVE' AND NOT confirmed AND expiry_requested_at IS NULL;

CREATE TABLE settlements (
    trade_id    uuid NOT NULL,
    trader_id   uuid NOT NULL REFERENCES accounts,
    order_id    uuid NOT NULL,
    ticker      text NOT NULL,
    side        text NOT NULL,
    quantity    integer NOT NULL,
    price_paise bigint NOT NULL,
    executed_at timestamptz NOT NULL,
    PRIMARY KEY (trade_id, trader_id)
);

CREATE TABLE last_prices (
    ticker      text PRIMARY KEY,
    price_paise bigint NOT NULL,
    updated_at  timestamptz NOT NULL
);

-- Idempotency: inserted in the same transaction as the change the event caused.
CREATE TABLE processed_events (
    event_id     uuid PRIMARY KEY,
    event_type   text NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT now()
);
