"""The order store (Cassandra keyspace order_service; data ownership doc)."""

from dataclasses import dataclass
from typing import Protocol

from order_service.domain.order import Order
from order_service.runtime.events import EventEnvelope

TOPIC = "orders.v1"


@dataclass(frozen=True)
class OutboxEntry:
    """An event waiting in the outbox for orders.v1 (key = ticker)."""

    order_id: str
    ticker: str
    event: EventEnvelope

    @property
    def event_type(self) -> str:
        return self.event.event_type


class OrderStore(Protocol):
    async def get(self, order_id: str) -> Order | None: ...

    async def create(self, order: Order, outbox: OutboxEntry | None) -> None:
        """Store a new order, its first event and (if given) the outbox row, all or nothing."""

    async def save_status(
        self, orders: list[Order], *, processed_event_id: str | None, cancel_flag: bool
    ) -> None:
        """Write the status columns (and, if cancel_flag, cancel_requested) of these orders, plus
        the processed event, all or nothing. Never touches the columns the API writes."""

    async def set_cancel_requested(self, order_id: str, now: str, outbox: OutboxEntry) -> None:
        """The API's write: cancel_requested = true, plus the OrderCancelRequested outbox row."""

    async def add_outbox(self, entry: OutboxEntry) -> None: ...

    async def find_event(self, order_id: str, event_type: str) -> EventEnvelope | None: ...

    async def pending_outbox(self, since: str) -> list[OutboxEntry]:
        """Unpublished rows written at or after `since`, oldest first."""

    async def mark_published(self, entry: OutboxEntry) -> None: ...

    async def drop_outbox(self, entry: OutboxEntry) -> None:
        """Remove a row that must never be published (its order was rejected)."""

    async def claim_idempotency_key(self, trader_id: str, key: str, order_id: str) -> str:
        """Bind (trader, key) to order_id for 24 h unless bound; return the bound order_id."""

    async def orders_of(
        self, trader_id: str, *, before: tuple[str, str] | None, limit: int
    ) -> list[Order]:
        """The trader's orders, newest first, strictly older than `before` (placed_at, order_id)."""

    async def is_processed(self, event_id: str) -> bool: ...
