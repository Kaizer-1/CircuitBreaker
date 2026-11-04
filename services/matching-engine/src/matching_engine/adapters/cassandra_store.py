"""The EventStore port on Cassandra (keyspace matching_engine; ADR-0004, data ownership doc).

A command's events and its command_id are written in one LOGGED batch, so after a crash either all
of them are stored or none (the batch log replays a half-written batch). The driver is synchronous,
so every call runs in a worker thread.
"""

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, date
from importlib import resources
from typing import Any

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
            "AND sequence > ?",
            "select_snapshots": "SELECT as_of, state FROM book_snapshots WHERE ticker = ?",
            "insert_snapshot": "INSERT INTO book_snapshots (ticker, sequence, as_of, state) "
            "VALUES (?, ?, ?, ?)",
            "select_cursor": "SELECT sequence FROM publish_cursor WHERE ticker = ?",
            "upsert_cursor": "INSERT INTO publish_cursor (ticker, sequence) VALUES (?, ?)",
        }
        for name, cql in queries.items():
            self._statements[name] = await asyncio.to_thread(self._session.prepare, cql)

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
        found: list[EventEnvelope] = []
        for day, _ in days[start:]:
            rows = await self._run(self._prepared("select_events"), (ticker, day, after_sequence))
            for row in rows:
                event = EventEnvelope.from_json(row.payload.encode())
                if cut is not None and parse_timestamp(event.occurred_at) > cut:
                    return found  # sequence order is time order: nothing later can qualify
                found.append(event)
                if limit is not None and len(found) >= limit:
                    return found
        return found

    async def latest_snapshot(self, ticker: str, at: str | None = None) -> dict[str, Any] | None:
        cut = parse_timestamp(at) if at is not None else None
        for row in await self._run(self._prepared("select_snapshots"), (ticker,)):  # newest first
            if cut is None or _as_utc(row.as_of) <= cut:
                snapshot: dict[str, Any] = json.loads(row.state)
                return snapshot
        return None

    async def save_snapshot(self, ticker: str, snapshot: dict[str, Any]) -> None:
        await self._run(
            self._prepared("insert_snapshot"),
            (
                ticker,
                snapshot["sequence"],
                parse_timestamp(snapshot["as_of"]),
                json.dumps(snapshot),
            ),
        )

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
