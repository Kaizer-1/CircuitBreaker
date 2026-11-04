"""In-memory fakes for order-service's ports, with the same column rules as the Cassandra store:
save_status never writes cancel_requested unless asked, and set_cancel_requested writes only it."""

from dataclasses import replace

from order_service.domain.order import Order, OrderRequest, OrderType, RejectionReason, Side
from order_service.ports.order_store import OutboxEntry
from order_service.ports.portfolio import (
    DependencyUnavailableError,
    ReservationRefusedError,
    ReservationResult,
)
from order_service.runtime.events import EventEnvelope, parse_timestamp

STATUS_FIELDS = (
    "status",
    "filled_quantity",
    "filled_value_paise",
    "cancel_reason",
    "rejection_reason",
    "last_sequence",
    "updated_at",
)


class InMemoryOrderStore:
    def __init__(self) -> None:
        self.orders: dict[str, Order] = {}
        self.events: dict[str, list[EventEnvelope]] = {}  # order_events, per order
        self.outbox: list[OutboxEntry] = []
        self.keys: dict[tuple[str, str], str] = {}
        self.processed: set[str] = set()
        self.fail_create = False

    async def get(self, order_id: str) -> Order | None:
        order = self.orders.get(order_id)
        return replace(order) if order else None

    async def create(self, order: Order, outbox: OutboxEntry | None) -> None:
        if self.fail_create:
            raise TimeoutError("cassandra write timed out")
        self.orders[order.order_id] = replace(order)
        if outbox is not None:
            await self.add_outbox(outbox)

    async def save_status(
        self, orders: list[Order], *, processed_event_id: str | None, cancel_flag: bool
    ) -> None:
        for order in orders:
            stored = self.orders[order.order_id]
            for name in STATUS_FIELDS + (("cancel_requested",) if cancel_flag else ()):
                setattr(stored, name, getattr(order, name))
        if processed_event_id is not None:
            self.processed.add(processed_event_id)

    async def set_cancel_requested(self, order_id: str, now: str, outbox: OutboxEntry) -> None:
        self.orders[order_id].cancel_requested = True
        self.orders[order_id].updated_at = now
        await self.add_outbox(outbox)

    async def add_outbox(self, entry: OutboxEntry) -> None:
        self.events.setdefault(entry.order_id, []).append(entry.event)
        if all(e.event.event_id != entry.event.event_id for e in self.outbox):
            self.outbox.append(entry)

    async def find_event(self, order_id: str, event_type: str) -> EventEnvelope | None:
        found = [e for e in self.events.get(order_id, []) if e.event_type == event_type]
        return found[0] if found else None

    async def pending_outbox(self, since: str) -> list[OutboxEntry]:
        cut = parse_timestamp(since)
        return [e for e in self.outbox if parse_timestamp(e.event.occurred_at) >= cut]

    async def mark_published(self, entry: OutboxEntry) -> None:
        self.outbox = [e for e in self.outbox if e.event.event_id != entry.event.event_id]
        if entry.event_type == "OrderPlaced" and entry.order_id in self.orders:
            self.orders[entry.order_id].published = True

    async def drop_outbox(self, entry: OutboxEntry) -> None:
        self.outbox = [e for e in self.outbox if e.event.event_id != entry.event.event_id]

    async def claim_idempotency_key(self, trader_id: str, key: str, order_id: str) -> str:
        return self.keys.setdefault((trader_id, key), order_id)

    async def orders_of(
        self, trader_id: str, *, before: tuple[str, str] | None, limit: int
    ) -> list[Order]:
        mine = sorted(
            (o for o in self.orders.values() if o.trader_id == trader_id),
            key=lambda o: (o.placed_at, o.order_id),
            reverse=True,
        )
        if before is not None:
            mine = [o for o in mine if (o.placed_at, o.order_id) < before]
        return [replace(o) for o in mine[:limit]]

    async def is_processed(self, event_id: str) -> bool:
        return event_id in self.processed


class FakePortfolio:
    """A-06: reserves until a trader's budget runs out; can be made unavailable."""

    def __init__(self, budget_paise: int = 10_000_000) -> None:
        self.budget = budget_paise
        self.calls: list[str] = []
        self.down = False

    async def reserve(
        self, order_id: str, trader_id: str, request: OrderRequest
    ) -> ReservationResult:
        self.calls.append(order_id)
        if self.down:
            raise DependencyUnavailableError("portfolio-service did not answer")
        if request.side is Side.SELL and request.quantity > 100:
            raise ReservationRefusedError(RejectionReason.INSUFFICIENT_SHARES, "not enough shares")
        protection = 27005 if request.order_type is OrderType.MARKET else None
        price = request.limit_price_paise or protection or 0
        if request.side is Side.BUY and request.quantity * price > self.budget:
            raise ReservationRefusedError(RejectionReason.INSUFFICIENT_FUNDS, "not enough cash")
        return ReservationResult(f"c1d2e3f4-0000-4000-8000-{len(self.calls):012d}", protection)


class RecordingPublisher:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, EventEnvelope]] = []
        self.down = False

    async def publish(self, topic: str, key: str, event: EventEnvelope) -> None:
        if self.down:
            raise ConnectionError("redpanda is down")
        self.sent.append((topic, key, event))

    def types(self) -> list[str]:
        return [e.event_type for _, _, e in self.sent]
