-- Q-07 status=open (pre-chaos hardening, 2026-11-14): a query-shaped table holding only the orders that may
-- still trade (Order.is_open), one partition per trader, newest first. Written in the same batch as the
-- order's status, so the two never disagree; listing costs what is open, not the trader's whole history.
-- A closed order's row is deleted, which leaves a tombstone in that trader's partition: leveled compaction
-- merges a partition's SSTables often (reads stay on few files), and a tombstone may be purged an hour
-- after it was written (gc_grace_seconds). One hour is safe here: one Cassandra node, replication factor 1,
-- so no replica can miss the delete and bring the row back.
CREATE TABLE IF NOT EXISTS open_orders_by_trader (
    trader_id text,
    placed_at timestamp,
    order_id  text,
    PRIMARY KEY (trader_id, placed_at, order_id)
) WITH CLUSTERING ORDER BY (placed_at DESC, order_id DESC)
  AND compaction = {'class': 'LeveledCompactionStrategy', 'unchecked_tombstone_compaction': 'true',
                    'tombstone_compaction_interval': '3600'}
  AND gc_grace_seconds = 3600;

-- Data migrations already done (the open-orders backfill): a name per migration, written once it finished.
CREATE TABLE IF NOT EXISTS data_migrations (
    name    text PRIMARY KEY,
    done_at timestamp
);
