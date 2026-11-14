-- Q-07 status=open (pre-chaos hardening, 2026-11-14): a query-shaped table holding only the orders that may
-- still trade (Order.is_open), one partition per trader, newest first. Written in the same batch as the
-- order's status, so the two never disagree; listing costs what is open, not the trader's whole history.
-- A closed order's row is deleted, which leaves a tombstone in that trader's partition until compaction
-- purges it; a listing reads the tombstones of its partition, so they must not pile up. Three settings:
-- the table is flushed to disk every minute (a tombstone still in memory can never be purged), a tombstone
-- may be purged 10 minutes after it was written (gc_grace_seconds), and a file may be compacted on its own
-- for its tombstones once it is 10 minutes old. Ten minutes is safe: one Cassandra node, replication
-- factor 1, so no replica can miss the delete, and far longer than a LOGGED batch takes to be replayed
-- after a failure (seconds), which could otherwise bring a purged row back. The 60-minute soak measured
-- the cost without the flush and with an hour: up to 8,239 tombstones per listing for a load account that
-- places and cancels twice a second.
CREATE TABLE IF NOT EXISTS open_orders_by_trader (
    trader_id text,
    placed_at timestamp,
    order_id  text,
    PRIMARY KEY (trader_id, placed_at, order_id)
) WITH CLUSTERING ORDER BY (placed_at DESC, order_id DESC);
ALTER TABLE open_orders_by_trader WITH gc_grace_seconds = 600
  AND memtable_flush_period_in_ms = 60000
  AND compaction = {'class': 'LeveledCompactionStrategy', 'unchecked_tombstone_compaction': 'true',
                    'tombstone_compaction_interval': '600'};

-- Data migrations already done (the open-orders backfill): a name per migration, written once it finished.
CREATE TABLE IF NOT EXISTS data_migrations (
    name    text PRIMARY KEY,
    done_at timestamp
);
