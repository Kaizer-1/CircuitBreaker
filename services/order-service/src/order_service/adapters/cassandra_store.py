"""The OrderStore port on Cassandra (keyspace order_service).

Writes that must happen together (an order, its first event and its outbox row) are one LOGGED
batch. The driver is synchronous, so every call runs in a worker thread.
"""

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from importlib import resources
from typing import Any

from order_service.domain.order import Order, OrderStatus, OrderType, RejectionReason, Side
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
    "select_outbox": "SELECT * FROM outbox WHERE bucket = ? AND occurred_at >= ?",
    "delete_outbox": "DELETE FROM outbox WHERE bucket = ? AND occurred_at = ? AND event_id = ?",
    "claim_key": "INSERT INTO orders_by_idempotency_key (trader_id, idempotency_key, order_id)"
    " VALUES (?, ?, ?) IF NOT EXISTS",
    "select_page": "SELECT order_id FROM orders_by_trader WHERE trader_id = ? LIMIT ?",
    "select_page_before": "SELECT order_id FROM orders_by_trader WHERE trader_id = ?"
    " AND (placed_at, order_id) < (?, ?) LIMIT ?",
    "select_processed": "SELECT event_id FROM processed_events WHERE event_id = ?",
    "insert_processed": "INSERT INTO processed_events (event_id, processed_at) VALUES (?, ?)",
}


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
        protocol_version=5,
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


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


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


class CassandraOrderStore:
    def __init__(self, session: Any) -> None:
        self._session = session
        self._q: dict[str, Any] = {}

    async def _run(self, query: Any, params: Sequence[Any] = ()) -> list[Any]:
        return await asyncio.to_thread(lambda: list(self._session.execute(query, params)))

    async def _batch(self, statements: list[tuple[str, tuple[Any, ...]]]) -> None:
        from cassandra.query import BatchStatement, BatchType

        batch = BatchStatement(batch_type=BatchType.LOGGED)
        for name, params in statements:
            batch.add(self._q[name], params)
        await asyncio.to_thread(self._session.execute, batch)

    async def migrate(self) -> None:
        for statement in migration_statements():
            await self._run(statement)
        for name, cql in QUERIES.items():
            self._q[name] = await asyncio.to_thread(self._session.prepare, cql)

    # ------------------------------------------------------------------ orders

    async def get(self, order_id: str) -> Order | None:
        rows = await self._run(self._q["select_order"], (order_id,))
        return _order(rows[0]) if rows else None

    def _event_row(self, order_id: str, event: EventEnvelope) -> tuple[str, tuple[Any, ...]]:
        at = parse_timestamp(event.occurred_at)
        payload = event.to_json().decode()
        return "insert_event", (order_id, at, event.event_id, event.event_type, payload, False)

    def _outbox_row(self, entry: OutboxEntry) -> tuple[str, tuple[Any, ...]]:
        at = parse_timestamp(entry.event.occurred_at)
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

        return await asyncio.to_thread(claim)

    async def orders_of(
        self, trader_id: str, *, before: tuple[str, str] | None, limit: int
    ) -> list[Order]:
        if before is None:
            rows = await self._run(self._q["select_page"], (trader_id, limit))
        else:
            placed_at, order_id = before
            params = (trader_id, parse_timestamp(placed_at), order_id, limit)
            rows = await self._run(self._q["select_page_before"], params)
        found = await asyncio.gather(*(self.get(r.order_id) for r in rows))
        return [o for o in found if o is not None]

    # ------------------------------------------------------------------ outbox and events

    async def add_outbox(self, entry: OutboxEntry) -> None:
        await self._batch([self._event_row(entry.order_id, entry.event), self._outbox_row(entry)])

    async def find_event(self, order_id: str, event_type: str) -> EventEnvelope | None:
        for row in await self._run(self._q["select_events"], (order_id,)):
            if row.event_type == event_type:
                return EventEnvelope.from_json(row.payload.encode())
        return None

    async def pending_outbox(self, since: str) -> list[OutboxEntry]:
        start = parse_timestamp(since)
        last = bucket_of(datetime.now(UTC) + timedelta(minutes=1))
        found: list[OutboxEntry] = []
        for bucket in range(bucket_of(start), last + 1):
            for row in await self._run(self._q["select_outbox"], (bucket, start)):
                event = EventEnvelope.from_json(row.payload.encode())
                found.append(OutboxEntry(row.order_id, row.ticker, event))
        return found

    async def mark_published(self, entry: OutboxEntry) -> None:
        at = parse_timestamp(entry.event.occurred_at)
        statements: list[tuple[str, tuple[Any, ...]]] = [
            ("delete_outbox", (bucket_of(at), at, entry.event.event_id)),
            ("event_published", (entry.order_id, at, entry.event.event_id)),
        ]
        if entry.event_type == "OrderPlaced":
            statements.append(("set_published", (entry.order_id,)))
        await self._batch(statements)

    async def drop_outbox(self, entry: OutboxEntry) -> None:
        at = parse_timestamp(entry.event.occurred_at)
        await self._run(self._q["delete_outbox"], (bucket_of(at), at, entry.event.event_id))

    async def is_processed(self, event_id: str) -> bool:
        return bool(await self._run(self._q["select_processed"], (event_id,)))
