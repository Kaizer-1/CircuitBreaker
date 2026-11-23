"""The OrderStore port on Cassandra (keyspace order_service).

Writes that must happen together (an order, its first event and its outbox row) are one LOGGED
batch. The driver is synchronous, so every call runs in a worker thread.

The outbox (pre-chaos hardening, 2026-11-14) is written once and never deleted from: a row that is
published, or must never be, gets `done = true`, and every row expires an hour after it was written
(the table's TTL; migrations/004). Nothing is deleted, so the relay never reads a tombstone of its
own making. It never reads old rows again either; each pass reads only

1. the rows written since the newest row this replica has seen (new rows),
2. the slice that has just settled (older than the relay's clock margin): a row written late, with
   a time already behind (1), is found here, each row once, and
3. the rows it saw that are not done yet, one partition read per minute that holds them.

A minute partition holds at most a minute of rows, and the cursor (the oldest minute that may still
hold a waiting row) is where a restarted replica starts. A request waiting longer than the TTL is
written again by the relay before it expires (application/outbox_relay.py), so a row of any age is
still found.
"""

import asyncio
import json
import logging
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from importlib import resources
from typing import Any, TypeVar

from order_service.domain.order import (
    FINISHED,
    Order,
    OrderStatus,
    OrderType,
    RejectionReason,
    Side,
)
from order_service.ports.order_store import OutboxEntry
from order_service.runtime.events import EventEnvelope, format_timestamp, parse_timestamp

_STATUS = (
    "UPDATE orders_by_id SET status = ?, filled_quantity = ?, filled_value_paise = ?,"
    " cancel_reason = ?, rejection_reason = ?, last_sequence = ?, updated_at = ?"
)
QUERIES = {
    "insert_order": "INSERT INTO orders_by_id (order_id, trader_id, ticker, side, order_type,"
    " quantity, limit_price_paise, protection_price_paise, reservation_id, status,"
    " filled_quantity, filled_value_paise, cancel_requested, cancel_reason, rejection_reason,"
    " last_sequence, published, placed_at, updated_at)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    "insert_by_trader": "INSERT INTO orders_by_trader (trader_id, placed_at, order_id)"
    " VALUES (?, ?, ?)",
    "select_order": "SELECT * FROM orders_by_id WHERE order_id = ?",
    "update_status": _STATUS + " WHERE order_id = ?",
    "update_status_and_cancel": _STATUS + ", cancel_requested = ? WHERE order_id = ?",
    "set_cancel": "UPDATE orders_by_id SET cancel_requested = true, updated_at = ?"
    " WHERE order_id = ?",
    "set_published": "UPDATE orders_by_id SET published = true WHERE order_id = ?",
    "insert_event": "INSERT INTO order_events (order_id, occurred_at, event_id, event_type,"
    " payload, published) VALUES (?, ?, ?, ?, ?, ?)",
    "event_published": "UPDATE order_events SET published = true WHERE order_id = ?"
    " AND occurred_at = ? AND event_id = ?",
    "select_events": "SELECT event_type, payload FROM order_events WHERE order_id = ?",
    "insert_outbox": "INSERT INTO outbox (bucket, occurred_at, event_id, order_id, ticker,"
    " payload) VALUES (?, ?, ?, ?, ?, ?)",
    "outbox_new": "SELECT bucket, occurred_at, event_id, done FROM outbox WHERE bucket = ?"
    " AND (occurred_at, event_id) > (?, ?)",
    "outbox_bucket": "SELECT bucket, occurred_at, event_id, done FROM outbox WHERE bucket = ?",
    "outbox_settled": "SELECT bucket, occurred_at, event_id, done FROM outbox WHERE bucket = ?"
    " AND occurred_at > ? AND occurred_at <= ?",
    "outbox_waiting": "SELECT * FROM outbox WHERE bucket = ? AND (occurred_at, event_id) >= (?, ?)",
    "select_cursor": "SELECT bucket FROM outbox_cursor WHERE name = 'relay'",
    "save_cursor": "INSERT INTO outbox_cursor (name, bucket) VALUES ('relay', ?)",
    "outbox_buckets": "SELECT DISTINCT bucket FROM outbox",
    "outbox_done": "UPDATE outbox SET done = true WHERE bucket = ? AND occurred_at = ?"
    " AND event_id = ?",
    "claim_key": "INSERT INTO orders_by_idempotency_key (trader_id, idempotency_key, order_id)"
    " VALUES (?, ?, ?) IF NOT EXISTS",
    "insert_open": "INSERT INTO open_orders_by_trader (trader_id, placed_at, order_id)"
    " VALUES (?, ?, ?)",
    "delete_open": "DELETE FROM open_orders_by_trader WHERE trader_id = ? AND placed_at = ?"
    " AND order_id = ?",
    "select_open": "SELECT order_id FROM open_orders_by_trader WHERE trader_id = ? LIMIT ?",
    "select_open_before": "SELECT order_id FROM open_orders_by_trader WHERE trader_id = ?"
    " AND (placed_at, order_id) < (?, ?) LIMIT ?",
    "scan_orders": "SELECT order_id, trader_id, placed_at, status, WRITETIME(status) AS written"
    " FROM orders_by_id",
    "backfill_open": "INSERT INTO open_orders_by_trader (trader_id, placed_at, order_id)"
    " VALUES (?, ?, ?) USING TIMESTAMP ?",
    "scan_open": "SELECT order_id FROM open_orders_by_trader",
    "select_migration": "SELECT name FROM data_migrations WHERE name = ?",
    "save_migration": "INSERT INTO data_migrations (name, done_at) VALUES (?, ?)",
    "select_page": "SELECT order_id FROM orders_by_trader WHERE trader_id = ? LIMIT ?",
    "select_page_before": "SELECT order_id FROM orders_by_trader WHERE trader_id = ?"
    " AND (placed_at, order_id) < (?, ?) LIMIT ?",
    "select_processed": "SELECT event_id FROM processed_events WHERE event_id = ?",
    "insert_processed": "INSERT INTO processed_events (event_id, processed_at) VALUES (?, ?)",
}


logger = logging.getLogger("order_store")
FINISHED_VALUES = {status.value for status in FINISHED}
OUTBOX_TTL = timedelta(hours=1)  # the outbox table's default_time_to_live (migrations/004)
SCAN_READS_AT_ONCE = 8  # Q-07 listings, across all callers; the worker pool has about 32 threads


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
    statements: list[str] = []
    folder = resources.files("order_service") / "migrations"
    for file in sorted(
        (f for f in folder.iterdir() if f.name.endswith(".cql")), key=lambda f: f.name
    ):
        text = "\n".join(
            line.split("--")[0] for line in file.read_text().splitlines() if line.strip()
        )
        statements += [s.strip() for s in text.split(";") if s.strip()]
    return statements


def bucket_of(moment: datetime) -> int:
    return int(moment.timestamp() // 60)


def bucket_start(bucket: int) -> datetime:
    return datetime.fromtimestamp(bucket * 60, UTC)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _outbox_entry(row: Any) -> OutboxEntry:
    event = EventEnvelope.from_json(row.payload.encode())
    written = _utc(row.occurred_at)
    same = written == parse_timestamp(event.occurred_at)
    return OutboxEntry(
        row.order_id, row.ticker, event, written_at=None if same else format_timestamp(written)
    )


def _order(row: Any) -> Order:
    return Order(
        order_id=row.order_id,
        trader_id=row.trader_id,
        ticker=row.ticker,
        side=Side(row.side),
        order_type=OrderType(row.order_type),
        quantity=row.quantity,
        limit_price_paise=row.limit_price_paise,
        protection_price_paise=row.protection_price_paise,
        reservation_id=row.reservation_id,
        status=OrderStatus(row.status),
        placed_at=format_timestamp(_utc(row.placed_at)),
        updated_at=format_timestamp(_utc(row.updated_at)),
        filled_quantity=row.filled_quantity or 0,
        filled_value_paise=row.filled_value_paise or 0,
        cancel_requested=bool(row.cancel_requested),
        cancel_reason=row.cancel_reason,
        rejection_reason=RejectionReason(row.rejection_reason) if row.rejection_reason else None,
        last_sequence=row.last_sequence or 0,
        published=bool(row.published),
    )


T = TypeVar("T")


class CassandraOrderStore:
    def __init__(
        self,
        session: Any,
        *,
        on_call: Callable[[float, float], None] = lambda waited, ran: None,
    ) -> None:
        self._session = session
        # Each blocking driver call runs on a worker thread: (seconds waiting for a free thread,
        # seconds the call itself took), so a slow call can be told apart from a queue for threads.
        self._on_call = on_call
        self._q: dict[str, Any] = {}
        self._scan_slots = asyncio.Semaphore(SCAN_READS_AT_ONCE)
        # The outbox relay's reading position (see the module docstring), per replica:
        self._seen: tuple[int, datetime, str] | None = None  # newest row read (1)
        self._swept: datetime | None = None  # every row up to this time was read settled (2)
        self._waiting: set[tuple[int, datetime, str]] = set()  # rows read, not done yet (3)
        self._cursor: int | None = None  # as last saved

    async def _threaded(self, call: Callable[[], T]) -> T:
        submitted = time.perf_counter()

        def timed() -> T:
            started = time.perf_counter()
            try:
                return call()
            finally:
                self._on_call(started - submitted, time.perf_counter() - started)

        return await asyncio.to_thread(timed)

    async def _run(self, query: Any, params: Sequence[Any] = ()) -> list[Any]:
        return await self._threaded(lambda: list(self._session.execute(query, params)))

    async def _batch(self, statements: list[tuple[str, tuple[Any, ...]]]) -> None:
        from cassandra.query import BatchStatement, BatchType

        batch = BatchStatement(batch_type=BatchType.LOGGED)
        for name, params in statements:
            batch.add(self._q[name], params)
        await self._threaded(lambda: self._session.execute(batch))

    async def migrate(self) -> None:
        for statement in migration_statements():
            await self._run(statement)
        for name, cql in QUERIES.items():
            self._q[name] = await asyncio.to_thread(self._session.prepare, cql)
        await self._backfill_open_orders()

    async def _backfill_open_orders(self) -> None:
        """Once: every open order stored before the open-orders table existed joins it.

        Each row is written with the timestamp of its order's status (WRITETIME), so a close that
        lands while this runs (a later timestamp) still wins, in whichever order the two arrive:
        the table cannot end up holding an order that has already closed. Replicas may run it at
        the same time; the writes are idempotent. One read of every order, page by page, once."""
        name = "open_orders_backfill"
        if await self._run(self._q["select_migration"], (name,)):
            return

        def scan() -> tuple[int, list[tuple[Any, ...]]]:
            # The driver pages through the table (5,000 rows a page): only open orders are kept,
            # so a long history never sits in memory at once.
            seen, writes = 0, []
            for r in self._session.execute(self._q["scan_orders"]):
                seen += 1
                if r.status not in FINISHED_VALUES and r.trader_id and r.placed_at:
                    writes.append((r.trader_id, r.placed_at, r.order_id, r.written))
            return seen, writes

        seen, writes = await asyncio.to_thread(scan)
        for params in writes:
            await self._run(self._q["backfill_open"], params)
        await self._run(self._q["save_migration"], (name, datetime.now(UTC)))
        logger.info("open orders backfilled", extra={"orders": seen, "open": len(writes)})

    # ------------------------------------------------------------------ orders

    async def get(self, order_id: str) -> Order | None:
        rows = await self._run(self._q["select_order"], (order_id,))
        return _order(rows[0]) if rows else None

    def _event_row(self, order_id: str, event: EventEnvelope) -> tuple[str, tuple[Any, ...]]:
        at = parse_timestamp(event.occurred_at)
        payload = event.to_json().decode()
        return "insert_event", (order_id, at, event.event_id, event.event_type, payload, False)

    def _outbox_row(self, entry: OutboxEntry) -> tuple[str, tuple[Any, ...]]:
        at = parse_timestamp(entry.row_at)
        payload = entry.event.to_json().decode()
        params = (bucket_of(at), at, entry.event.event_id, entry.order_id, entry.ticker, payload)
        return "insert_outbox", params

    async def create(self, order: Order, outbox: OutboxEntry | None) -> None:
        placed = parse_timestamp(order.placed_at)
        statements: list[tuple[str, tuple[Any, ...]]] = [
            (
                "insert_order",
                (
                    order.order_id,
                    order.trader_id,
                    order.ticker,
                    order.side.value,
                    order.order_type.value,
                    order.quantity,
                    order.limit_price_paise,
                    order.protection_price_paise,
                    order.reservation_id,
                    order.status.value,
                    order.filled_quantity,
                    order.filled_value_paise,
                    order.cancel_requested,
                    order.cancel_reason,
                    order.rejection_reason.value if order.rejection_reason else None,
                    order.last_sequence,
                    order.published,
                    placed,
                    parse_timestamp(order.updated_at),
                ),
            ),
            ("insert_by_trader", (order.trader_id, placed, order.order_id)),
        ]
        if order.is_open:
            statements.append(("insert_open", (order.trader_id, placed, order.order_id)))
        if outbox is not None:
            statements += [self._event_row(order.order_id, outbox.event), self._outbox_row(outbox)]
        else:  # local event OrderRejected: stored, never published
            rejected = json.dumps(
                {"event_type": "OrderRejected", "reason": str(order.rejection_reason)}
            )
            statements.append(
                (
                    "insert_event",
                    (order.order_id, placed, order.order_id, "OrderRejected", rejected, False),
                )
            )
        await self._batch(statements)

    async def save_status(
        self, orders: list[Order], *, processed_event_id: str | None, cancel_flag: bool
    ) -> None:
        statements: list[tuple[str, tuple[Any, ...]]] = []
        for o in orders:
            values: tuple[Any, ...] = (
                o.status.value,
                o.filled_quantity,
                o.filled_value_paise,
                o.cancel_reason,
                o.rejection_reason.value if o.rejection_reason else None,
                o.last_sequence,
                parse_timestamp(o.updated_at),
            )
            if cancel_flag:
                statements.append(
                    ("update_status_and_cancel", (*values, o.cancel_requested, o.order_id))
                )
            else:
                statements.append(("update_status", (*values, o.order_id)))
            # The open-orders row follows the status in the same batch (same timestamp), so
            # whichever status write wins also decides the row; a re-opened order comes back.
            index = (o.trader_id, parse_timestamp(o.placed_at), o.order_id)
            statements.append(("insert_open" if o.is_open else "delete_open", index))
        if processed_event_id is not None:
            statements.append(("insert_processed", (processed_event_id, datetime.now(UTC))))
        if statements:
            await self._batch(statements)

    async def set_cancel_requested(self, order_id: str, now: str, outbox: OutboxEntry) -> None:
        await self._batch(
            [
                ("set_cancel", (parse_timestamp(now), order_id)),
                self._event_row(order_id, outbox.event),
                self._outbox_row(outbox),
            ]
        )

    async def claim_idempotency_key(self, trader_id: str, key: str, order_id: str) -> str:
        def claim() -> str:
            result = self._session.execute(self._q["claim_key"], (trader_id, key, order_id))
            return order_id if result.was_applied else str(result.one().order_id)

        return await self._threaded(claim)

    async def orders_of(
        self, trader_id: str, *, before: tuple[str, str] | None, limit: int
    ) -> list[Order]:
        if before is None:
            rows = await self._run(self._q["select_page"], (trader_id, limit))
        else:
            placed_at, order_id = before
            params = (trader_id, parse_timestamp(placed_at), order_id, limit)
            rows = await self._run(self._q["select_page_before"], params)
        found = await asyncio.gather(*(self._scan_get(r.order_id) for r in rows))
        return [o for o in found if o is not None]

    async def open_orders_of(
        self, trader_id: str, *, before: tuple[str, str] | None, limit: int
    ) -> list[Order]:
        if before is None:
            rows = await self._run(self._q["select_open"], (trader_id, limit))
        else:
            placed_at, order_id = before
            params = (trader_id, parse_timestamp(placed_at), order_id, limit)
            rows = await self._run(self._q["select_open_before"], params)
        found = await asyncio.gather(*(self._scan_get(r.order_id) for r in rows))
        return [o for o in found if o is not None]

    async def open_orders(self) -> list[Order]:
        rows = await self._run(self._q["scan_open"])
        found = await asyncio.gather(*(self._scan_get(r.order_id) for r in rows))
        return [o for o in found if o is not None]

    async def _scan_get(self, order_id: str) -> Order | None:
        # Listing reads one row per order. Without a cap, a few long listings queue thousands of
        # reads in the worker threads and an order placement waits behind them.
        async with self._scan_slots:
            return await self.get(order_id)

    # ------------------------------------------------------------------ outbox and events

    async def add_outbox(self, entry: OutboxEntry) -> None:
        await self._batch([self._event_row(entry.order_id, entry.event), self._outbox_row(entry)])

    async def find_event(self, order_id: str, event_type: str) -> EventEnvelope | None:
        payloads = [
            row.payload
            for row in await self._run(self._q["select_events"], (order_id,))
            if row.event_type == event_type
        ]
        return EventEnvelope.from_json(payloads[-1].encode()) if payloads else None

    async def requeue(self, entry: OutboxEntry) -> None:
        await self._batch([self._outbox_row(entry)])

    async def pending_outbox(self, settled_before: str) -> list[OutboxEntry]:
        settled = parse_timestamp(settled_before)  # no row is written before this any more
        last = bucket_of(datetime.now(UTC) + timedelta(minutes=1))  # replicas' clocks differ
        if self._seen is None or self._swept is None:
            first = await self._start(bucket_of(settled))
            before_first = bucket_start(first) - timedelta(milliseconds=1)
            self._seen, self._swept = (first, before_first, ""), before_first
        # (1) New rows: after the newest row seen, to the newest minute.
        seen_bucket, seen_at, seen_id = self._seen
        for bucket in range(seen_bucket, last + 1):
            if bucket == seen_bucket:
                rows = await self._run(self._q["outbox_new"], (bucket, seen_at, seen_id))
            else:
                rows = await self._run(self._q["outbox_bucket"], (bucket,))
            for row in rows:
                self._note(row)
                self._seen = max(self._seen, (row.bucket, _utc(row.occurred_at), row.event_id))
        # (2) The slice that settled since the last pass: rows written late are found here.
        if settled > self._swept:
            for bucket in range(bucket_of(self._swept), bucket_of(settled) + 1):
                params = (bucket, self._swept, settled)
                for row in await self._run(self._q["outbox_settled"], params):
                    self._note(row)
            self._swept = settled
        # (3) What is still waiting: one read per minute that holds such rows.
        found: list[OutboxEntry] = []
        for bucket in sorted({key[0] for key in self._waiting}):
            oldest = min(key for key in self._waiting if key[0] == bucket)
            rows = await self._run(self._q["outbox_waiting"], oldest)
            current = {(row.bucket, _utc(row.occurred_at), row.event_id): row for row in rows}
            for key in sorted(k for k in self._waiting if k[0] == bucket):
                row = current.get(key)
                if row is None or row.done or row.payload is None:
                    self._waiting.discard(key)  # done, or expired
                else:
                    found.append(_outbox_entry(row))
        cursor = min([bucket_of(self._swept), *(key[0] for key in self._waiting)])
        if cursor != self._cursor:
            await self._run(self._q["save_cursor"], (cursor,))
            self._cursor = cursor
        return sorted(found, key=lambda e: parse_timestamp(e.row_at))

    def _note(self, row: Any) -> None:
        if not row.done:
            self._waiting.add((row.bucket, _utc(row.occurred_at), row.event_id))

    async def _start(self, settled: int) -> int:
        """Where the relay stopped last time, but never further back than the rows' TTL (older
        rows have expired). With no cursor yet (a store from before Phase 4), the oldest minute that
        holds a row (one scan of the partition keys, once), and never later than the settled
        minute: a row may still be written up to the clock margin in the past."""
        oldest_alive = bucket_of(datetime.now(UTC) - OUTBOX_TTL)
        rows = await self._run(self._q["select_cursor"])
        if rows:
            self._cursor = rows[0].bucket
            return max(int(rows[0].bucket), oldest_alive)
        buckets = [r.bucket for r in await self._run(self._q["outbox_buckets"])]
        return max(min([*(int(b) for b in buckets), settled]), oldest_alive)

    async def mark_published(self, entry: OutboxEntry) -> None:
        row_at = parse_timestamp(entry.row_at)
        occurred_at = parse_timestamp(entry.event.occurred_at)
        statements: list[tuple[str, tuple[Any, ...]]] = [
            ("outbox_done", (bucket_of(row_at), row_at, entry.event.event_id)),
            ("event_published", (entry.order_id, occurred_at, entry.event.event_id)),
        ]
        if entry.event_type == "OrderPlaced":
            statements.append(("set_published", (entry.order_id,)))
        await self._batch(statements)
        self._waiting.discard((bucket_of(row_at), row_at, entry.event.event_id))

    async def drop_outbox(self, entry: OutboxEntry) -> None:
        at = parse_timestamp(entry.row_at)
        await self._run(self._q["outbox_done"], (bucket_of(at), at, entry.event.event_id))
        self._waiting.discard((bucket_of(at), at, entry.event.event_id))

    async def is_processed(self, event_id: str) -> bool:
        return bool(await self._run(self._q["select_processed"], (event_id,)))
