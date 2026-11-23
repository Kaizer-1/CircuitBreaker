"""The EventStore port on Cassandra (keyspace matching_engine; ADR-0004, data ownership doc).

A command's events and its command_id are written in one LOGGED batch, so after a crash either all
of them are stored or none (the batch log replays a half-written batch). The driver is synchronous,
so every call runs in a worker thread.

Reads stay cheap however long the market has traded. Event reads carry a CQL `LIMIT` and stop at
the page size or the `until` cut, and the rows are parsed in the worker thread, so a long Q-14 page
never holds up the command loop. Picking a snapshot reads only the (sequence, as_of) of the
snapshots in one hour's partition (two at most), then loads the one state it needs.

Snapshots are kept by domain/retention.py: after each new one the engine calls `prune_snapshots`,
which drops hour partitions past the horizon and thins hours older than the last one. The first
snapshot table (one partition per ticker, never pruned) is moved over once at start-up and dropped.
"""

import asyncio
import json
import logging
from collections.abc import Sequence
from datetime import UTC, date, datetime
from importlib import resources
from typing import Any

from matching_engine.domain.model import ms_of
from matching_engine.domain.retention import (
    FULL_WINDOW_MS,
    HOUR_MS,
    hour_of,
    hours_to_drop,
    snapshots_to_keep,
)
from matching_engine.runtime.events import EventEnvelope, parse_timestamp


def connect_cassandra(
    contact_points: Sequence[str], port: int, keyspace: str, username: str, password: str
) -> Any:
    """A session on this service's own keyspace, logged in with this service's own role."""
    from cassandra.auth import PlainTextAuthProvider
    from cassandra.cluster import Cluster
    from cassandra.io.asyncioreactor import AsyncioConnection

    cluster = Cluster(
        list(contact_points),
        port=port,
        auth_provider=PlainTextAuthProvider(username, password),
        connection_class=AsyncioConnection,
        # v4, not v5: under concurrent load, v5's segment framing failed its CRC with the asyncio
        # reactor (CrcException, connection defunct, host marked down: seen with the Phase 3 bots).
        protocol_version=4,
        connect_timeout=5,
    )
    return cluster.connect(keyspace)


def migration_statements() -> list[str]:
    """Every CQL statement in matching_engine/migrations/*.cql, in file order."""
    statements: list[str] = []
    folder = resources.files("matching_engine") / "migrations"
    for file in sorted(
        (f for f in folder.iterdir() if f.name.endswith(".cql")), key=lambda f: f.name
    ):
        text = "\n".join(
            line for line in file.read_text().splitlines() if not line.strip().startswith("--")
        )
        statements += [s.strip() for s in text.split(";") if s.strip()]
    return statements


logger = logging.getLogger("event-store")

_NO_LIMIT = 2**31 - 1  # CQL LIMIT is an int; recovery reads everything after the snapshot


def _day(occurred_at: str) -> date:
    return parse_timestamp(occurred_at).date()


class CassandraEventStore:
    def __init__(self, session: Any) -> None:
        self._session = session
        self._statements: dict[str, Any] = {}
        self._days: dict[str, set[date]] = {}  # day partitions known per ticker (single writer)

    async def _run(self, query: Any, params: Sequence[Any] = ()) -> list[Any]:
        return await asyncio.to_thread(lambda: list(self._session.execute(query, params)))

    def _prepared(self, name: str) -> Any:
        return self._statements[name]

    async def migrate(self) -> None:
        for statement in migration_statements():
            await self._run(statement)
        queries = {
            "insert_event": "INSERT INTO market_events (ticker, day, sequence, event_id, "
            "event_type, occurred_at, payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
            "insert_day": "INSERT INTO ticker_days (ticker, day, first_sequence) VALUES (?, ?, ?)",
            "insert_command": "INSERT INTO processed_commands (command_id, ticker, processed_at) "
            "VALUES (?, ?, toTimestamp(now()))",
            "select_command": "SELECT command_id FROM processed_commands WHERE command_id = ?",
            "select_days": "SELECT day, first_sequence FROM ticker_days WHERE ticker = ?",
            "select_events": "SELECT payload FROM market_events WHERE ticker = ? AND day = ? "
            "AND sequence > ? LIMIT ?",
            "select_hours": "SELECT hour, thinned FROM snapshot_hours WHERE ticker = ?",
            "select_hours_until": "SELECT hour FROM snapshot_hours WHERE ticker = ? AND hour <= ?",
            "insert_hour": "INSERT INTO snapshot_hours (ticker, hour) VALUES (?, ?)",
            "mark_thinned": "UPDATE snapshot_hours SET thinned = true "
            "WHERE ticker = ? AND hour = ?",
            "delete_hour": "DELETE FROM snapshot_hours WHERE ticker = ? AND hour = ?",
            "select_snapshot_times": "SELECT sequence, as_of FROM book_snapshots_by_hour "
            "WHERE ticker = ? AND hour = ?",
            "select_snapshot": "SELECT state FROM book_snapshots_by_hour "
            "WHERE ticker = ? AND hour = ? AND sequence = ?",
            "select_newest_snapshot": "SELECT state FROM book_snapshots_by_hour "
            "WHERE ticker = ? AND hour = ? LIMIT 1",
            "insert_snapshot": "INSERT INTO book_snapshots_by_hour (ticker, hour, sequence, as_of, "
            "state) VALUES (?, ?, ?, ?, ?)",
            "delete_snapshot": "DELETE FROM book_snapshots_by_hour "
            "WHERE ticker = ? AND hour = ? AND sequence = ?",
            "delete_hour_snapshots": "DELETE FROM book_snapshots_by_hour "
            "WHERE ticker = ? AND hour = ?",
            "select_cursor": "SELECT sequence FROM publish_cursor WHERE ticker = ?",
            "upsert_cursor": "INSERT INTO publish_cursor (ticker, sequence) VALUES (?, ?)",
        }
        for name, cql in queries.items():
            self._statements[name] = await asyncio.to_thread(self._session.prepare, cql)
        await self._move_first_snapshot_table()

    async def _move_first_snapshot_table(self) -> None:
        """Copy what retention keeps of the first snapshot table, then drop it (once).

        That table held every snapshot of a ticker in one partition (2 GB after 19 hours). It is
        read one ticker at a time, only (sequence, as_of) in small pages, and each kept state by
        its key, so no read pulls a whole partition through the heap. A crash midway is safe: the
        copy is repeated (same rows) and the table is dropped only at the end. Cassandra keeps the
        dropped table's files as a backup (auto_snapshot) until `nodetool clearsnapshot`.
        """
        from cassandra.query import SimpleStatement

        found = await self._run(
            "SELECT table_name FROM system_schema.tables "
            "WHERE keyspace_name = %s AND table_name = 'book_snapshots'",
            (self._session.keyspace,),
        )
        if not found:
            return
        tickers = [r.ticker for r in await self._run("SELECT DISTINCT ticker FROM book_snapshots")]
        for ticker in tickers:
            times = await self._run(
                SimpleStatement(
                    "SELECT sequence, as_of FROM book_snapshots WHERE ticker = %s", fetch_size=100
                ),
                (ticker,),
            )
            rows = [(r.sequence, _ms(r.as_of)) for r in times]
            if not rows:
                continue
            now = max(a for _, a in rows)
            keep = snapshots_to_keep(rows, now)
            for sequence, as_of in rows:
                if sequence not in keep:
                    continue
                state = await self._run(
                    "SELECT state FROM book_snapshots WHERE ticker = %s AND sequence = %s",
                    (ticker, sequence),
                )
                await self._insert_snapshot(ticker, sequence, as_of, state[0].state)
            for hour in {hour_of(a) for s, a in rows if s in keep}:
                if hour + HOUR_MS <= now - FULL_WINDOW_MS:
                    await self._run(self._prepared("mark_thinned"), (ticker, _dt(hour)))
            logger.info(
                "old snapshots moved",
                extra={"ticker": ticker, "snapshots": len(rows), "kept": len(keep)},
            )
        await self._run("DROP TABLE IF EXISTS book_snapshots")
        logger.warning(
            "old snapshot table dropped; its files stay as a Cassandra backup until a person runs "
            "nodetool clearsnapshot"
        )

    async def _known_days(self, ticker: str) -> set[date]:
        if ticker not in self._days:
            rows = await self._run(self._prepared("select_days"), (ticker,))
            self._days[ticker] = {_as_date(r.day) for r in rows}
        return self._days[ticker]

    async def append(self, ticker: str, events: Sequence[EventEnvelope], command_id: str) -> None:
        from cassandra.query import BatchStatement, BatchType

        batch = BatchStatement(batch_type=BatchType.LOGGED)
        known = await self._known_days(ticker)
        new_days: set[date] = set()
        for event in events:
            day = _day(event.occurred_at)
            if day not in known and day not in new_days:
                new_days.add(day)
                batch.add(self._prepared("insert_day"), (ticker, day, event.data["sequence"]))
            batch.add(
                self._prepared("insert_event"),
                (
                    ticker,
                    day,
                    event.data["sequence"],
                    event.event_id,
                    event.event_type,
                    parse_timestamp(event.occurred_at),
                    event.to_json().decode(),
                ),
            )
        batch.add(self._prepared("insert_command"), (command_id, ticker))
        await asyncio.to_thread(self._session.execute, batch)
        known |= new_days

    async def is_processed(self, command_id: str) -> bool:
        return bool(await self._run(self._prepared("select_command"), (command_id,)))

    async def events_after(
        self,
        ticker: str,
        after_sequence: int,
        *,
        limit: int | None = None,
        until: str | None = None,
    ) -> list[EventEnvelope]:
        days = sorted(
            (_as_date(r.day), r.first_sequence)
            for r in await self._run(self._prepared("select_days"), (ticker,))
        )
        # Start at the last day whose first event is not after the one we want.
        start = max([i for i, (_, first) in enumerate(days) if first <= after_sequence + 1] or [0])
        cut = parse_timestamp(until) if until is not None else None
        wanted = limit if limit is not None else _NO_LIMIT
        query = self._prepared("select_events")

        def read() -> list[EventEnvelope]:
            # One worker thread reads and parses the rows in clustering (sequence) order, day after
            # day, into one list, so the order is the store's order. The driver fetches pages
            # lazily: reading stops at the limit or at the first event after the cut.
            found: list[EventEnvelope] = []
            for day, _ in days[start:]:
                rows = self._session.execute(
                    query, (ticker, day, after_sequence, wanted - len(found))
                )
                for row in rows:
                    event = EventEnvelope.from_json(row.payload.encode())
                    if cut is not None and parse_timestamp(event.occurred_at) > cut:
                        return found  # sequence order is time order: nothing later can qualify
                    found.append(event)
                if len(found) >= wanted:
                    break
            return found

        return await asyncio.to_thread(read)

    async def latest_snapshot(self, ticker: str, at: str | None = None) -> dict[str, Any] | None:
        if at is None:
            hours = await self._run(self._prepared("select_hours"), (ticker,))
            for row in hours:  # newest first; an hour emptied by a crash midway is skipped
                found = await self._run(
                    self._prepared("select_newest_snapshot"), (ticker, row.hour)
                )
                if found:
                    return _state(found[0].state)
            return None
        cut = parse_timestamp(at)
        until = _dt(hour_of(ms_of(at)))
        for row in await self._run(self._prepared("select_hours_until"), (ticker, until)):
            # Newest first; only (sequence, as_of), not every snapshot's whole book. T's own hour
            # may hold only later snapshots: then the hour before has one at or before T.
            times = await self._run(self._prepared("select_snapshot_times"), (ticker, row.hour))
            sequence = next((r.sequence for r in times if _as_utc(r.as_of) <= cut), None)
            if sequence is not None:
                found = await self._run(
                    self._prepared("select_snapshot"), (ticker, row.hour, sequence)
                )
                return _state(found[0].state) if found else None
        return None

    async def save_snapshot(self, ticker: str, snapshot: dict[str, Any]) -> None:
        await self._insert_snapshot(
            ticker, snapshot["sequence"], ms_of(snapshot["as_of"]), json.dumps(snapshot)
        )

    async def _insert_snapshot(self, ticker: str, sequence: int, as_of: int, state: str) -> None:
        # The hour first: a crash in between leaves a listed hour with no snapshot (skipped by
        # reads), never a snapshot no read can find.
        hour = _dt(hour_of(as_of))
        await self._run(self._prepared("insert_hour"), (ticker, hour))
        await self._run(
            self._prepared("insert_snapshot"), (ticker, hour, sequence, _dt(as_of), state)
        )

    async def prune_snapshots(self, ticker: str, now: str) -> int:
        now_ms = ms_of(now)
        hours = {
            _ms(r.hour): bool(r.thinned)
            for r in await self._run(self._prepared("select_hours"), (ticker,))
        }
        if not hours:
            return 0
        deleted = 0
        for hour in sorted(hours_to_drop(hours, now_ms)):
            # The partition first: a crash in between leaves a listed, empty hour (skipped).
            await self._run(self._prepared("delete_hour_snapshots"), (ticker, _dt(hour)))
            await self._run(self._prepared("delete_hour"), (ticker, _dt(hour)))
            del hours[hour]
            deleted += 1
        full_from = now_ms - FULL_WINDOW_MS
        open_hours = [h for h, thinned in hours.items() if not thinned and h < full_from]
        if not open_hours:
            return deleted
        # The newest hour too: the newest snapshot is always kept, wherever it is.
        read = sorted(set(open_hours) | {max(hours)})
        rows: list[tuple[int, int, int]] = []
        for hour in read:
            times = await self._run(self._prepared("select_snapshot_times"), (ticker, _dt(hour)))
            rows += [(hour, r.sequence, _ms(r.as_of)) for r in times]
        keep = snapshots_to_keep(((s, a) for _, s, a in rows), now_ms)
        for hour, sequence, _ in rows:
            if sequence not in keep:
                await self._run(self._prepared("delete_snapshot"), (ticker, _dt(hour), sequence))
                deleted += 1
        for hour in open_hours:
            if hour + HOUR_MS <= full_from:
                await self._run(self._prepared("mark_thinned"), (ticker, _dt(hour)))
        return deleted

    async def publish_cursor(self, ticker: str) -> int:
        rows = await self._run(self._prepared("select_cursor"), (ticker,))
        return int(rows[0].sequence) if rows else 0

    async def set_publish_cursor(self, ticker: str, sequence: int) -> None:
        await self._run(self._prepared("upsert_cursor"), (ticker, sequence))


def _as_date(value: Any) -> date:
    """The driver returns cassandra.util.Date for `date` columns."""
    return value if isinstance(value, date) else value.date()


def _as_utc(value: Any) -> Any:
    """The driver returns naive UTC datetimes for `timestamp` columns."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _ms(value: Any) -> int:
    return int(_as_utc(value).timestamp() * 1000)


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def _state(text: str) -> dict[str, Any]:
    snapshot: dict[str, Any] = json.loads(text)
    return snapshot
