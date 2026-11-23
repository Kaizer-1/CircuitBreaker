-- Book snapshots (policy P-03; local event BookSnapshotTaken), one partition per ticker per hour of
-- as_of, newest first, so no partition holds more than an hour of them. The engine deletes what
-- domain/retention.py does not keep (single node: tombstones may go after an hour).
CREATE TABLE IF NOT EXISTS book_snapshots_by_hour (
    ticker   text,
    hour     timestamp,  -- the start of the hour that holds as_of
    sequence bigint,
    as_of    timestamp,
    state    text,  -- BookSnapshot.to_dict() as JSON
    PRIMARY KEY ((ticker, hour), sequence)
) WITH CLUSTERING ORDER BY (sequence DESC) AND gc_grace_seconds = 3600;

-- Which hour partitions hold snapshots, newest first; thinned once the whole hour is older than the
-- hour in which every snapshot is kept.
CREATE TABLE IF NOT EXISTS snapshot_hours (
    ticker  text,
    hour    timestamp,
    thinned boolean,
    PRIMARY KEY (ticker, hour)
) WITH CLUSTERING ORDER BY (hour DESC) AND gc_grace_seconds = 3600;
