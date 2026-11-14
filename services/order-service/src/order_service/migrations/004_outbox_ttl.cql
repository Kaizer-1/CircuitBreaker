-- The outbox without deletes (pre-chaos hardening, 2026-11-14). Until now a published row was deleted, and every
-- relay pass re-read the last minutes' partitions: about 2,000 tombstones per read under load. Now a row is
-- marked done instead (a normal write, no tombstone) and expires an hour after it was written (the TTL), and
-- the relay reads only what is new (adapters/cassandra_store.py).
-- Compaction: leveled. Size-tiered would keep an hour of rows in few large files; time-window compaction
-- suits rows that only ever expire, but a row marked done after its window closed would keep that window's
-- files (and every newer one) from being dropped. Leveled compaction merges a partition's versions across
-- time, so the done mark and the expired rows are cleaned up. gc_grace_seconds one hour: expired rows may be
-- purged an hour later; safe with one node (replication factor 1) and no deletes.
ALTER TABLE outbox ADD IF NOT EXISTS done boolean;
ALTER TABLE outbox WITH default_time_to_live = 3600
  AND gc_grace_seconds = 3600
  AND compaction = {'class': 'LeveledCompactionStrategy', 'unchecked_tombstone_compaction': 'true',
                    'tombstone_compaction_interval': '3600'};
