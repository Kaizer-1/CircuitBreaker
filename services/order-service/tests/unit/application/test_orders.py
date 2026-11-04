"""Use cases A-05, A-07, A-19, Q-07, Q-08 and the matching.v1 status updates."""

from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

import pytest

from order_service.application.orders import (
    NotCancellableError,
    OrderNotFoundError,
    Orders,
    RejectedOrderError,
)
from order_service.application.outbox_relay import OutboxRelay
from order_service.domain.order import (
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    OrderValidationError,
    RejectionReason,
    Side,
)
from order_service.ports.portfolio import DependencyUnavailableError
from order_service.runtime.events import EventEnvelope, build_event, deterministic_event_id
from tests.fakes import FakePortfolio, InMemoryOrderStore, RecordingPublisher

RAJU, MEERA = "3d2a1b0c-9e8f-4a7b-8c6d-5e4f3a2b1c0d", "5e6f7a8b-9c0d-4e1f-8a2b-3c4d5e6f7a8b"
START = datetime(2026, 11, 20, 10, 42, 1, 248_000, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


class World:
    def __init__(self) -> None:
        self.clock = Clock()
        self.store = InMemoryOrderStore()
        self.portfolio = FakePortfolio()
        self.publisher = RecordingPublisher()
        ids = count(1)
        self.orders = Orders(
            self.store,
            self.portfolio,
            self.publisher,
            environment="dev",
            clock=self.clock,
            new_id=lambda: f"0dde0000-0000-4000-8000-{next(ids):012d}",
        )
        self.relay = OutboxRelay(self.store, self.publisher, clock=self.clock)
        self._seq = count(1000)

    async def place(self, trader: str = RAJU, **overrides: Any) -> Order:
        fields: dict[str, Any] = {
            "ticker": "CHAIWALA",
            "side": Side.BUY,
            "order_type": OrderType.LIMIT,
            "quantity": 50,
            "limit_price_paise": 24600,
        } | overrides
        return await self.orders.place(
            trader,
            OrderRequest(**fields),
            correlation_id="req-1",
        )

    def engine(self, event_type: str, **data: object) -> EventEnvelope:
        return build_event(
            event_type,
            {"sequence": next(self._seq), "ticker": "CHAIWALA", **data},
            producer="matching-engine",
            environment="dev",
            correlation_id="req-1",
        )


@pytest.fixture
def w() -> World:
    return World()


# ------------------------------------------------------------------ A-05


async def test_placing_an_order_reserves_stores_and_publishes_order_placed(w: World) -> None:
    order = await w.place()
    assert (order.status, order.reservation_id) == (
        OrderStatus.PENDING,
        "c1d2e3f4-0000-4000-8000-000000000001",
    )
    [(topic, key, event)] = w.publisher.sent
    assert (topic, key, event.event_type, event.producer) == (
        "orders.v1",
        "CHAIWALA",
        "OrderPlaced",
        "order-service",
    )
    assert event.data["reservation_id"] == order.reservation_id
    assert event.data["limit_price_paise"] == 24600 and event.data["protection_price_paise"] is None
    assert w.store.outbox == []  # published at once, so nothing is left for the relay
    assert w.store.orders[order.order_id].published


async def test_a_market_order_carries_the_protection_price_from_its_reservation(w: World) -> None:
    order = await w.place(order_type=OrderType.MARKET, limit_price_paise=None, quantity=10)
    assert order.protection_price_paise == 27005
    assert w.publisher.sent[0][2].data["protection_price_paise"] == 27005


async def test_an_invalid_order_reserves_nothing_and_stores_nothing(w: World) -> None:
    with pytest.raises(OrderValidationError):
        await w.place(limit_price_paise=24603)
    assert (w.portfolio.calls, w.store.orders) == ([], {})


async def test_not_enough_cash_stores_a_rejected_order_and_publishes_nothing(w: World) -> None:
    with pytest.raises(RejectedOrderError) as rejected:
        await w.place(quantity=10_000)
    stored = w.store.orders[rejected.value.order.order_id]
    assert (stored.status, stored.rejection_reason) == (
        OrderStatus.REJECTED,
        RejectionReason.INSUFFICIENT_FUNDS,
    )
    assert w.publisher.sent == []


async def test_portfolio_down_means_503_and_nothing_stored(w: World) -> None:
    w.portfolio.down = True
    with pytest.raises(DependencyUnavailableError):
        await w.place()
    assert w.store.orders == {}


async def test_a_store_failure_after_reserving_is_503_and_leaves_an_orphan_for_the_sweeper(
    w: World,
) -> None:
    # order-service "dies" between reserving and storing: the money stays reserved, no order exists.
    w.store.fail_create = True
    with pytest.raises(DependencyUnavailableError):
        await w.place()
    assert len(w.portfolio.calls) == 1 and w.store.orders == {}
    # 120 s later portfolio-service asks for an expiry (A-19); order-service has never heard of it.
    w.clock.now += timedelta(seconds=120)
    accepted = await w.orders.request_expiry(w.portfolio.calls[0], RAJU, "CHAIWALA")
    assert accepted.known_order is False
    [(_, _, event)] = w.publisher.sent
    assert event.event_type == "OrderExpiryRequested"


async def test_if_publishing_fails_the_order_is_still_accepted_and_the_relay_sends_it(
    w: World,
) -> None:
    w.publisher.down = True
    order = await w.place()
    assert order.status is OrderStatus.PENDING and len(w.store.outbox) == 1
    w.publisher.down = False
    assert await w.relay.run_once() == 0  # younger than 5 s: the request path may still be on it
    w.clock.now += timedelta(seconds=5)
    assert await w.relay.run_once() == 1
    assert w.publisher.types() == ["OrderPlaced"] and w.store.orders[order.order_id].published


async def test_the_same_idempotency_key_returns_the_same_order(w: World) -> None:
    request = OrderRequest("CHAIWALA", Side.BUY, OrderType.LIMIT, 5, 24600)
    first = await w.orders.place(RAJU, request, correlation_id="a", idempotency_key="phone-retry-1")
    again = await w.orders.place(RAJU, request, correlation_id="b", idempotency_key="phone-retry-1")
    assert again.order_id == first.order_id
    assert len(w.store.orders) == 1 and len(w.publisher.sent) == 1
    other = await w.orders.place(
        MEERA, request, correlation_id="c", idempotency_key="phone-retry-1"
    )
    assert other.order_id != first.order_id  # keys are per trader


async def test_a_retry_after_a_crash_between_claiming_the_key_and_storing_finishes_that_order(
    w: World,
) -> None:
    request = OrderRequest("CHAIWALA", Side.BUY, OrderType.LIMIT, 5, 24600)
    w.store.fail_create = True
    with pytest.raises(DependencyUnavailableError):
        await w.orders.place(RAJU, request, correlation_id="a", idempotency_key="k")
    w.store.fail_create = False
    order = await w.orders.place(RAJU, request, correlation_id="b", idempotency_key="k")
    assert w.portfolio.calls == [order.order_id, order.order_id]  # A-06 is idempotent per order_id


async def test_a_retry_of_a_rejected_order_is_rejected_again(w: World) -> None:
    request = OrderRequest("CHAIWALA", Side.SELL, OrderType.LIMIT, 500, 24600)
    with pytest.raises(RejectedOrderError):
        await w.orders.place(RAJU, request, correlation_id="a", idempotency_key="k")
    with pytest.raises(RejectedOrderError) as again:
        await w.orders.place(RAJU, request, correlation_id="b", idempotency_key="k")
    assert again.value.order.rejection_reason is RejectionReason.INSUFFICIENT_SHARES
    assert len(w.portfolio.calls) == 1


# ------------------------------------------------------------------ A-07, Q-07, Q-08


async def test_only_the_owner_sees_or_cancels_an_order(w: World) -> None:
    order = await w.place()
    with pytest.raises(OrderNotFoundError):
        await w.orders.get(MEERA, order.order_id)
    with pytest.raises(OrderNotFoundError):
        await w.orders.cancel(MEERA, order.order_id, correlation_id="c")
    with pytest.raises(OrderNotFoundError):
        await w.orders.get(RAJU, "00000000-0000-4000-8000-000000000000")


async def test_a_cancel_is_requested_once_and_published_after_the_order(w: World) -> None:
    order = await w.place()
    cancelled = await w.orders.cancel(RAJU, order.order_id, correlation_id="c")
    assert cancelled.cancel_requested
    assert w.publisher.types() == ["OrderPlaced", "OrderCancelRequested"]
    with pytest.raises(NotCancellableError, match="already pending"):
        await w.orders.cancel(RAJU, order.order_id, correlation_id="c")


async def test_a_cancel_never_overtakes_an_unpublished_order(w: World) -> None:
    w.publisher.down = True
    order = await w.place()
    w.publisher.down = False
    await w.orders.cancel(RAJU, order.order_id, correlation_id="c")
    assert w.publisher.sent == []  # the cancel waits for the OrderPlaced
    w.clock.now += timedelta(seconds=6)
    assert await w.relay.run_once() == 2
    assert w.publisher.types() == ["OrderPlaced", "OrderCancelRequested"]


async def test_a_finished_order_cannot_be_cancelled(w: World) -> None:
    order = await w.place()
    await w.orders.apply_matching_event(w.engine("OrderAccepted", order_id=order.order_id))
    await w.orders.apply_matching_event(
        w.engine(
            "TradeExecuted",
            buy_order_id=order.order_id,
            sell_order_id="9e8d7c6b-5a4f-4e3d-8c2b-1a0f9e8d7c6b",
            quantity=50,
            price_paise=24550,
            buy_order_remaining_quantity=0,
            sell_order_remaining_quantity=0,
        )
    )
    with pytest.raises(NotCancellableError, match="FILLED"):
        await w.orders.cancel(RAJU, order.order_id, correlation_id="c")


async def test_listing_is_newest_first_with_filters_and_pages(w: World) -> None:
    placed = []
    for n in range(5):
        w.clock.now += timedelta(seconds=1)
        placed.append(
            await w.place(ticker="JUGAAD" if n == 2 else "CHAIWALA", limit_price_paise=24600)
        )
    await w.orders.apply_matching_event(
        w.engine(
            "OrderCancelled",
            order_id=placed[0].order_id,
            reason="USER_REQUESTED",
            cancelled_quantity=50,
        )
    )
    page, cursor = await w.orders.list(RAJU, status=None, ticker=None, limit=2, cursor=None)
    assert [o.order_id for o in page] == [placed[4].order_id, placed[3].order_id] and cursor
    rest, end = await w.orders.list(RAJU, status=None, ticker=None, limit=10, cursor=cursor)
    assert [o.order_id for o in rest] == [p.order_id for p in reversed(placed[:3])] and end is None
    open_orders, _ = await w.orders.list(RAJU, status="open", ticker=None, limit=10, cursor=None)
    assert placed[0].order_id not in {o.order_id for o in open_orders} and len(open_orders) == 4
    jugaad, _ = await w.orders.list(RAJU, status=None, ticker="JUGAAD", limit=10, cursor=None)
    assert [o.order_id for o in jugaad] == [placed[2].order_id]
    cancelled, _ = await w.orders.list(RAJU, status="CANCELLED", ticker=None, limit=10, cursor=None)
    assert [o.order_id for o in cancelled] == [placed[0].order_id]
    with pytest.raises(ValueError, match="cursor"):
        await w.orders.list(RAJU, status=None, ticker=None, limit=1, cursor="not-a-cursor")


# ------------------------------------------------------------------ A-19 (ADR-0017)


async def test_an_expiry_request_is_idempotent_with_a_deterministic_event_id(w: World) -> None:
    first = await w.orders.request_expiry("6c7d8e9f-0a1b-4c2d-8e3f-4a5b6c7d8e9f", RAJU, "CHAIWALA")
    w.clock.now += timedelta(seconds=30)
    again = await w.orders.request_expiry("6c7d8e9f-0a1b-4c2d-8e3f-4a5b6c7d8e9f", RAJU, "CHAIWALA")
    assert again.requested_at == first.requested_at
    [(_, key, event)] = w.publisher.sent
    assert key == "CHAIWALA"
    assert event.event_id == deterministic_event_id(
        "https://circuitbreaker.local/order-expiry/6c7d8e9f-0a1b-4c2d-8e3f-4a5b6c7d8e9f"
    )  # the catalog's example ID: a3b049e7-…


async def test_an_expiry_for_a_stored_but_never_published_order_rejects_it_first(w: World) -> None:
    # order-service stored the order, then could not publish for over two minutes.
    w.publisher.down = True
    order = await w.place()
    w.clock.now += timedelta(seconds=125)
    w.publisher.down = False
    accepted = await w.orders.request_expiry(order.order_id, RAJU, "CHAIWALA")
    assert accepted.known_order
    stored = w.store.orders[order.order_id]
    assert (stored.status, stored.rejection_reason) == (
        OrderStatus.REJECTED,
        RejectionReason.PUBLISH_TIMEOUT,
    )
    assert w.publisher.types() == ["OrderExpiryRequested"]
    assert await w.relay.run_once() == 0  # and the stale OrderPlaced is never sent
    assert w.publisher.types() == ["OrderExpiryRequested"] and w.store.outbox == []


# ------------------------------------------------------------------ matching.v1


async def test_status_follows_the_engine_and_duplicates_change_nothing(w: World) -> None:
    order = await w.place()
    accepted = w.engine("OrderAccepted", order_id=order.order_id)
    assert await w.orders.apply_matching_event(accepted)
    assert await w.orders.apply_matching_event(accepted) is False
    trade = w.engine(
        "TradeExecuted",
        buy_order_id=order.order_id,
        sell_order_id="9e8d7c6b-5a4f-4e3d-8c2b-1a0f9e8d7c6b",
        quantity=20,
        price_paise=24550,
        buy_order_remaining_quantity=30,
        sell_order_remaining_quantity=0,
    )
    assert await w.orders.apply_matching_event(trade)
    assert await w.orders.apply_matching_event(trade) is False
    o = await w.orders.get(RAJU, order.order_id)
    assert (o.status, o.filled_quantity, o.remaining_quantity, o.average_fill_price_paise) == (
        OrderStatus.PARTIALLY_FILLED,
        20,
        30,
        24550,
    )


async def test_a_rejected_cancel_clears_the_flag_and_a_cancel_closes_the_order(w: World) -> None:
    order = await w.place()
    await w.orders.cancel(RAJU, order.order_id, correlation_id="c")
    await w.orders.apply_matching_event(
        w.engine("OrderCancelRejected", order_id=order.order_id, reason="ALREADY_FILLED")
    )
    assert not (await w.orders.get(RAJU, order.order_id)).cancel_requested
    await w.orders.cancel(RAJU, order.order_id, correlation_id="c")
    await w.orders.apply_matching_event(
        w.engine(
            "OrderCancelled",
            order_id=order.order_id,
            reason="SELF_TRADE_PREVENTED",
            cancelled_quantity=50,
        )
    )
    o = await w.orders.get(RAJU, order.order_id)
    assert (o.status, o.cancel_reason, o.cancel_requested) == (
        OrderStatus.CANCELLED,
        "SELF_TRADE_PREVENTED",
        False,
    )


async def test_a_status_update_never_undoes_a_cancel_request_made_meanwhile(w: World) -> None:
    # The consumer read the order before the trader asked to cancel; its write must not clear it.
    order = await w.place()
    await w.orders.cancel(RAJU, order.order_id, correlation_id="c")
    await w.orders.apply_matching_event(w.engine("OrderAccepted", order_id=order.order_id))
    assert (await w.orders.get(RAJU, order.order_id)).cancel_requested


async def test_order_expired_and_events_for_unknown_orders(w: World) -> None:
    w.publisher.down = True
    order = await w.place()
    assert await w.orders.apply_matching_event(w.engine("OrderExpired", order_id=order.order_id))
    assert (await w.orders.get(RAJU, order.order_id)).status is OrderStatus.REJECTED
    assert await w.orders.apply_matching_event(
        w.engine("OrderAccepted", order_id="00000000-0000-4000-8000-000000000009")
    )
    other = build_event(
        "CandleClosed", {}, producer="market-data-service", environment="dev", correlation_id="x"
    )
    assert await w.orders.apply_matching_event(other) is False
