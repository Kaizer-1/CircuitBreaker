-- market-data-service read models (docs/03-architecture/02-data-ownership.md, keyspace market_data).
-- Every table can be dropped and rebuilt from matching-engine's event store (Q-14): this service
-- owns no facts, only views of them.

-- Q-03: candles of one ticker and interval, newest first. The forming candle is stored too.
CREATE TABLE IF NOT EXISTS candles (
    ticker text,
    interval text,
    bucket_start timestamp,
    open_paise bigint,
    high_paise bigint,
    low_paise bigint,
    close_paise bigint,
    volume bigint,
    trade_count int,
    PRIMARY KEY ((ticker, interval), bucket_start)
) WITH CLUSTERING ORDER BY (bucket_start DESC);

-- Q-05: recent trades of one ticker, one partition per UTC day, newest first.
CREATE TABLE IF NOT EXISTS trades_by_ticker (
    ticker text,
    day date,
    executed_at timestamp,
    trade_id uuid,
    sequence bigint,
    price_paise bigint,
    quantity int,
    aggressor_side text,
    PRIMARY KEY ((ticker, day), executed_at, trade_id)
) WITH CLUSTERING ORDER BY (executed_at DESC, trade_id ASC);

-- Q-02: last price and today's figures per ticker, plus `sequence`, the last matching.v1 event
-- applied (anything at or below it is a duplicate), and when the candle series began.
CREATE TABLE IF NOT EXISTS last_prices (
    ticker text PRIMARY KEY,
    sequence bigint,
    last_price_paise bigint,
    last_trade_at timestamp,
    day date,
    day_open_paise bigint,
    day_high_paise bigint,
    day_low_paise bigint,
    day_volume bigint,
    updated_at timestamp
);
