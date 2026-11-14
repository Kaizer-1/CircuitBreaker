"""Use cases: place (A-05), cancel (A-07), list and get (Q-07, Q-08), request an expiry (A-19), and
keep each order's status from matching.v1.

Placing an order (sequence diagram 2): validate → reserve with portfolio-service (A-06, synchronous)
→ store the order and its OrderPlaced in the outbox in one write → try to publish at once. If that
publish fails the outbox relay retries it; once the API has answered 202 the order *will* reach the
engine (or be REJECTED with PUBLISH_TIMEOUT after 60 s, never later).
"""

import asyncio
import base64
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import uuid4

from order_service.domain.order import (
    FINISHED,
    Order,
    OrderRequest,
    OrderStatus,
    RejectionReason,
    validate,
)
from order_service.events.orders import order_cancel_requested, order_expiry_requested, order_placed
from order_service.ports.order_store import TOPIC, OrderStore, OutboxEntry
from order_service.ports.portfolio import (
    DependencyUnavailableError,
    Portfolio,
    ReservationRefusedError,
)
from order_service.ports.publisher import OrderPublisher
from order_service.runtime.events import EventEnvelope, format_timestamp

logger = logging.getLogger("orders")

# The request path tries to publish once, briefly; if Redpanda is slow or down the relay takes over,
# so a trader never waits on the broker (the gateway would time out first).
PUBLISH_ATTEMPT_SECONDS = 1.0


class OrderNotFoundError(Exception):
    """No such order, or not the caller's (both answer 404, so others' orders stay invisible)."""


class NotCancellableError(Exception):
    def __init__(self, order: Order) -> None:
        super().__init__(f"Order {order.order_id} is already {order.status}.")


class RejectedOrderError(Exception):
    """The order was stored as REJECTED (local event OrderRejected); the API answers 422."""

    def __init__(self, order: Order, detail: str) -> None:
        super().__init__(detail)
        self.order, self.detail = order, detail


@dataclass(frozen=True)
class ExpiryAccepted:
    order_id: str
    requested_at: str
    known_order: bool


def must_wait(entry: OutboxEntry, order: Order | None) -> bool:
    """A cancel or expiry is never published before its order's OrderPlaced (same partition, so
    the engine would see it first and answer "unknown order"). Only a PENDING order may still be
    unseen: once the engine has answered (OPEN, a fill, a cancel) its OrderPlaced went out, even
    if `published` was never set (a publish attempt that timed out although Kafka delivered). A
    rejected order's OrderPlaced will never be published, so an expiry for it need not wait."""
    return (
        entry.event_type != "OrderPlaced"
        and order is not None
        and not order.published
        and order.status is OrderStatus.PENDING
    )


def encode_cursor(order: Order) -> str:
    return base64.urlsafe_b64encode(f"{order.placed_at}|{order.order_id}".encode()).decode()


def decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        placed_at, order_id = base64.urlsafe_b64decode(cursor.encode()).decode().split("|")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("invalid cursor") from exc
    return placed_at, order_id


class Orders:
    def __init__(
        self,
        store: OrderStore,
        portfolio: Portfolio,
        publisher: OrderPublisher,
        *,
        environment: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_id: Callable[[], str] = lambda: str(uuid4()),
        on_placed: Callable[[Order], None] = lambda order: None,
        on_rejected: Callable[[RejectionReason], None] = lambda reason: None,
        publish_attempt_seconds: float = PUBLISH_ATTEMPT_SECONDS,
    ) -> None:
        self._publish_attempt = publish_attempt_seconds
        self._store = store
        self._portfolio = portfolio
        self._publisher = publisher
        self._environment = environment
        self._clock = clock
        self._new_id = new_id
        self._on_placed = on_placed
        self._on_rejected = on_rejected

    def _now(self) -> str:
        return format_timestamp(self._clock())

    # ------------------------------------------------------------------ A-05

    async def place(
        self,
        trader_id: str,
        request: OrderRequest,
        *,
        correlation_id: str,
        idempotency_key: str | None = None,
    ) -> Order:
        validate(request)  # OrderValidationError: 422, nothing stored, nothing reserved
        order_id = self._new_id()
        if idempotency_key:
            order_id = await self._store.claim_idempotency_key(trader_id, idempotency_key, order_id)
            existing = await self._store.get(order_id)
            if existing is not None:  # a retry: the original result, not a second order
                if existing.status is OrderStatus.REJECTED:
                    raise RejectedOrderError(existing, f"Order {order_id} was rejected.")
                return existing
            # Claimed by an attempt that never stored its order: finish that attempt (A-06 is
            # idempotent per order_id, so its reservation, if any, is reused).

        at = self._clock()
        now = format_timestamp(at)
        order = Order(
            order_id=order_id,
            trader_id=trader_id,
            ticker=request.ticker,
            side=request.side,
            order_type=request.order_type,
            quantity=request.quantity,
            limit_price_paise=request.limit_price_paise,
            protection_price_paise=None,
            reservation_id=None,
            status=OrderStatus.PENDING,
            placed_at=now,
            updated_at=now,
        )
        try:
            reservation = await self._portfolio.reserve(order_id, trader_id, request)
        except ReservationRefusedError as refused:
            order.reject(refused.reason, now)
            await self._store.create(order, None)  # local event OrderRejected; never on the bus
            self._on_rejected(refused.reason)
            raise RejectedOrderError(order, refused.detail) from refused

        order.reservation_id = reservation.reservation_id
        order.protection_price_paise = reservation.protection_price_paise
        entry = OutboxEntry(
            order_id,
            order.ticker,
            order_placed(order, at, environment=self._environment, correlation_id=correlation_id),
        )
        try:
            await self._store.create(order, entry)
        except Exception as exc:
            # The cash or shares stay reserved; with no order stored, portfolio-service's sweeper
            # asks for an expiry after 120 s and the money comes back (ADR-0017).
            logger.exception("order store failed after reserving", extra={"order_id": order_id})
            raise DependencyUnavailableError("the order store is unavailable") from exc
        await self._publish_now(entry, order)
        self._on_placed(order)
        return order

    async def _publish_now(self, entry: OutboxEntry, order: Order | None) -> None:
        """One attempt. A failure leaves the row in the outbox for the relay."""
        if must_wait(entry, order):
            return  # its order's OrderPlaced has not gone out yet: the relay keeps the order
        try:
            await asyncio.wait_for(
                self._publisher.publish(TOPIC, entry.ticker, entry.event), self._publish_attempt
            )
            await self._store.mark_published(entry)
            if order is not None and entry.event_type == "OrderPlaced":
                order.published = True
        except Exception as exc:
            logger.warning(
                "publish failed; the outbox relay will retry",
                extra={"order_id": entry.order_id, "error": repr(exc)},
            )

    # ------------------------------------------------------------------ A-07, Q-07, Q-08

    async def get(self, trader_id: str, order_id: str) -> Order:
        order = await self._store.get(order_id)
        if order is None or order.trader_id != trader_id:
            raise OrderNotFoundError(order_id)
        return order

    async def cancel(self, trader_id: str, order_id: str, *, correlation_id: str) -> Order:
        order = await self.get(trader_id, order_id)
        if order.status in FINISHED:
            raise NotCancellableError(order)
        if order.cancel_requested:
            sent = await self._store.find_event(order_id, "OrderCancelRequested")
            if sent is not None:  # a retry while the engine has not answered yet
                await self._resend(OutboxEntry(order_id, order.ticker, sent), order)
                return order
        at = self._clock()
        now = format_timestamp(at)
        event = order_cancel_requested(
            order, at, environment=self._environment, correlation_id=correlation_id
        )
        entry = OutboxEntry(order_id, order.ticker, event)
        await self._store.set_cancel_requested(order_id, now, entry)
        order.cancel_requested, order.updated_at = True, now
        await self._publish_now(entry, order)
        return order

    async def _resend(self, entry: OutboxEntry, order: Order | None) -> None:
        """Send a stored request again: the same event, so the engine acts on it once (it drops a
        second copy by event_id). It goes back in the outbox first, so it is delivered even if
        Redpanda is down now. Cancels and expiries have no time limit (ADR-0017 note)."""
        again = replace(entry, written_at=self._now())
        await self._store.requeue(again)
        await self._publish_now(again, order)

    async def list(
        self,
        trader_id: str,
        *,
        status: str | None,
        ticker: str | None,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[Order], str | None]:
        """Newest first. `status` is a status or "open" (PENDING, OPEN, PARTIALLY_FILLED).

        "open" reads the open-orders table, so it costs what is open, however long the history;
        the pages are read until the filter (ticker) fills one. Any other filter walks the history:
        at most 10 pages of it per call, so one listing cannot keep a reader busy for long."""
        open_only = status == "open"

        def wanted(o: Order) -> bool:
            if ticker is not None and o.ticker != ticker:
                return False
            if open_only:
                return o.is_open
            return status is None or o.status == status

        read = self._store.open_orders_of if open_only else self._store.orders_of
        before = decode_cursor(cursor) if cursor else None
        found: list[Order] = []
        pages = 0
        while True:
            page = await read(trader_id, before=before, limit=limit)
            pages += 1
            found += [o for o in page if wanted(o)]
            if len(found) >= limit or len(page) < limit or (not open_only and pages == 10):
                break
            before = (page[-1].placed_at, page[-1].order_id)
        items = found[:limit]
        more = len(found) > limit or (len(items) == limit and len(page) == limit)
        return items, encode_cursor(items[-1]) if items and more else None

    # ------------------------------------------------------------------ A-19 (ADR-0017)

    async def request_expiry(self, order_id: str, trader_id: str, ticker: str) -> ExpiryAccepted:
        order = await self._store.get(order_id)
        existing = await self._store.find_event(order_id, "OrderExpiryRequested")
        if existing is not None:  # the sweeper asks again: the engine has not answered yet
            await self._resend(OutboxEntry(order_id, ticker, existing), order)
            requested_at = str(existing.data["requested_at"])
            return ExpiryAccepted(order_id, requested_at, order is not None)
        at = self._clock()
        now = format_timestamp(at)
        if order is not None and order.status is OrderStatus.PENDING and not order.published:
            # Older than 120 s and never published: it is past the 60 s limit, so it never will be.
            order.reject(RejectionReason.PUBLISH_TIMEOUT, now)
            await self._store.save_status([order], processed_event_id=None, cancel_flag=False)
        entry = OutboxEntry(
            order_id,
            ticker,
            order_expiry_requested(order_id, trader_id, ticker, at, environment=self._environment),
        )
        await self._store.add_outbox(entry)
        await self._publish_now(entry, order)
        logger.info("expiry requested", extra={"order_id": order_id, "known": order is not None})
        return ExpiryAccepted(order_id, now, order is not None)

    # ------------------------------------------------------------------ matching.v1 (policy P-04)

    async def apply_matching_event(self, envelope: EventEnvelope) -> bool:
        """Update the status of the order(s) an event is about. False for duplicates and others."""
        d, kind = envelope.data, envelope.event_type
        if kind not in {
            "OrderAccepted",
            "TradeExecuted",
            "OrderCancelled",
            "OrderCancelRejected",
            "OrderExpired",
        }:
            return False
        if await self._store.is_processed(envelope.event_id):
            return False
        now, sequence = self._now(), int(d["sequence"])
        changed: list[Order] = []
        cancel_flag = kind in {"OrderCancelled", "OrderCancelRejected"}
        if kind == "TradeExecuted":
            for order_id, remaining in (
                (d["buy_order_id"], d["buy_order_remaining_quantity"]),
                (d["sell_order_id"], d["sell_order_remaining_quantity"]),
            ):
                order = await self._store.get(order_id)
                if order is not None:
                    order.traded(sequence, now, d["quantity"], d["price_paise"], remaining)
                    changed.append(order)
                    cancel_flag = cancel_flag or remaining == 0
        else:
            order = await self._store.get(d["order_id"])
            if order is not None:
                if kind == "OrderAccepted":
                    order.accepted(sequence, now)
                elif kind == "OrderCancelled":
                    order.cancelled(sequence, now, d["reason"])
                elif kind == "OrderCancelRejected":
                    order.cancel_rejected(sequence, now)
                else:
                    order.expired(sequence, now)
                changed.append(order)
        await self._store.save_status(
            changed, processed_event_id=envelope.event_id, cancel_flag=cancel_flag
        )
        return True
