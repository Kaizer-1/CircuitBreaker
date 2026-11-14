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
    written_at: str | None = None  # when the row was written, if not with the event (a re-send)

    @property
    def event_type(self) -> str:
        return self.event.event_type

    @property
    def row_at(self) -> str:
        """The row's place in the outbox: when it was written."""
        return self.written_at or self.event.occurred_at


class OrderStore(Protocol):
    async def get(self, order_id: str) -> Order | None: ...

    async def create(self, order: Order, outbox: OutboxEntry | None) -> None:
        """Store a new order, its first event and (if given) the outbox row, all or nothing; an
        open order also joins the trader's open orders."""

    async def save_status(
        self, orders: list[Order], *, processed_event_id: str | None, cancel_flag: bool
    ) -> None:
        """Write the status columns (and, if cancel_flag, cancel_requested) of these orders, plus
        the processed event, all or nothing. Never touches the columns the API writes. In the same
        write, each order joins or leaves the trader's open orders (Order.is_open)."""

    async def set_cancel_requested(self, order_id: str, now: str, outbox: OutboxEntry) -> None:
        """The API's write: cancel_requested = true, plus the OrderCancelRequested outbox row."""

    async def add_outbox(self, entry: OutboxEntry) -> None: ...

    async def find_event(self, order_id: str, event_type: str) -> EventEnvelope | None:
        """The order's newest stored event of this type."""

    async def requeue(self, entry: OutboxEntry) -> None:
        """Put an already stored event back in the outbox, as a row written at entry.written_at
        (a retried A-07 or A-19 sends the same request again)."""

    async def pending_outbox(self, settled_before: str) -> list[OutboxEntry]:
        """Every unpublished row, however old, oldest first. No row is written before
        `settled_before` any more, so the store may skip what it found empty before it."""

    async def mark_published(self, entry: OutboxEntry) -> None: ...

    async def drop_outbox(self, entry: OutboxEntry) -> None:
        """Remove a row that must never be published (its order was rejected)."""

    async def claim_idempotency_key(self, trader_id: str, key: str, order_id: str) -> str:
        """Bind (trader, key) to order_id for 24 h unless bound; return the bound order_id."""

    async def orders_of(
        self, trader_id: str, *, before: tuple[str, str] | None, limit: int
    ) -> list[Order]:
        """The trader's orders, newest first, strictly older than `before` (placed_at, order_id)."""

    async def open_orders_of(
        self, trader_id: str, *, before: tuple[str, str] | None, limit: int
    ) -> list[Order]:
        """The trader's open orders only (Order.is_open), newest first, strictly older than
        `before`. Reads the open-orders table: the cost follows how many orders are open, not how
        many the trader ever placed."""

    async def open_orders(self) -> list[Order]:
        """Every trader's open orders (a scan of the open-orders table: what is open, not the
        history). The relay reads it once at start-up."""

    async def is_processed(self, event_id: str) -> bool: ...
