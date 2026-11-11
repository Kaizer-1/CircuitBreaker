"""The outbox relay: retry after 5 s, never publish an OrderPlaced older than 60 s (ADR-0011), and
never give up on a cancel or an expiry request (ADR-0017 note)."""

from datetime import timedelta

import pytest

from order_service.application.outbox_relay import OutboxRelay
from order_service.domain.order import OrderStatus, RejectionReason
from tests.unit.application.test_orders import RAJU, World


@pytest.fixture
def w() -> World:
    return World()


async def test_an_order_stored_but_never_published_is_rejected_after_60_seconds(w: World) -> None:
    # order-service stores the order, then cannot publish (it dies, or Redpanda is down).
    w.publisher.down = True
    await w.place()
    w.publisher.down = False
    w.clock.now += timedelta(seconds=49)
    assert await w.relay.run_once() == 1  # still in time: published
    assert w.publisher.types() == ["OrderPlaced"]

    late = World()
    late.publisher.down = True
    stuck = await late.place()
    late.publisher.down = False
    late.clock.now += timedelta(seconds=61)  # e.g. order-service restarted 61 s later
    assert await late.relay.run_once() == 0
    stored = late.store.orders[stuck.order_id]
    assert (stored.status, stored.rejection_reason) == (
        OrderStatus.REJECTED,
        RejectionReason.PUBLISH_TIMEOUT,
    )
    assert late.publisher.sent == [] and late.store.outbox == []  # never published, ever
    late.clock.now += timedelta(minutes=10)
    assert await late.relay.run_once() == 0 and late.publisher.sent == []


async def test_no_send_starts_in_the_last_10_seconds_so_nothing_lands_after_60(w: World) -> None:
    # The Kafka client may keep trying a send for up to 10 s after the relay stops waiting
    # (ADR-0017), so a send started at 51 s could reach the broker after 60 s.
    w.publisher.down = True
    order = await w.place()
    w.publisher.down = False
    w.clock.now += timedelta(seconds=51)
    assert await w.relay.run_once() == 0
    assert w.publisher.sent == []
    assert w.store.orders[order.order_id].status is OrderStatus.PENDING  # not rejected before 60 s
    w.clock.now += timedelta(seconds=10)
    assert await w.relay.run_once() == 0
    assert w.store.orders[order.order_id].status is OrderStatus.REJECTED
    assert w.publisher.sent == [] and w.store.outbox == []


async def test_a_cancel_is_still_sent_after_its_order_went_out(w: World) -> None:
    # The cut-off is only for OrderPlaced: a cancel or expiry has no age limit.
    order = await w.place()
    w.publisher.down = True
    await w.orders.cancel(RAJU, order.order_id, correlation_id="c")
    w.publisher.down = False
    w.clock.now += timedelta(seconds=58)
    assert await w.relay.run_once() == 1
    assert w.publisher.types() == ["OrderPlaced", "OrderCancelRequested"]


async def test_a_hung_broker_never_delays_the_60_second_rejection(w: World) -> None:
    w.relay = OutboxRelay(w.store, w.publisher, clock=w.clock, attempt_seconds=0.05)
    w.publisher.down = True
    early, late = await w.place(), None
    w.clock.now += timedelta(seconds=30)
    late = await w.place()
    w.publisher.down, w.publisher.hang = False, True  # Redpanda stops answering at all
    w.clock.now += timedelta(seconds=31)  # the first order is now 61 s old
    assert await w.relay.run_once() == 0
    assert w.store.orders[early.order_id].status is OrderStatus.REJECTED
    assert w.store.orders[late.order_id].status is OrderStatus.PENDING


async def test_a_pending_cancel_of_an_order_rejected_for_timeout_is_dropped(w: World) -> None:
    w.publisher.down = True
    order = await w.place()
    await w.orders.cancel(RAJU, order.order_id, correlation_id="c")
    w.publisher.down = False
    w.clock.now += timedelta(seconds=61)
    await w.relay.run_once()
    assert w.publisher.sent == [] and w.store.outbox == []
    assert w.store.orders[order.order_id].status is OrderStatus.REJECTED


async def test_while_the_broker_is_down_the_relay_keeps_everything_and_stops_the_pass(
    w: World,
) -> None:
    w.publisher.down = True
    await w.place()
    await w.place()
    w.clock.now += timedelta(seconds=10)
    assert await w.relay.run_once() == 0
    assert len(w.store.outbox) == 2
    w.publisher.down = False
    assert await w.relay.run_once() == 2


async def test_a_row_already_sent_by_another_replica_is_just_a_duplicate(w: World) -> None:
    # Two relays may publish the same row; the engine drops the second copy by event_id.
    w.publisher.down = True
    await w.place()
    w.publisher.down = False
    w.clock.now += timedelta(seconds=10)
    entry = w.store.outbox[0]
    await w.publisher.publish("orders.v1", entry.ticker, entry.event)  # the other replica
    await w.relay.run_once()
    assert [e.event_id for _, _, e in w.publisher.sent] == [entry.event.event_id] * 2


async def test_cancels_and_expiries_have_no_time_limit(w: World) -> None:
    # Until Phase 4 the relay looked back 15 minutes: after a longer outage a cancel was never sent,
    # its order rested for ever and the money stayed reserved.
    order = await w.place()
    w.publisher.down = True
    await w.orders.cancel(RAJU, order.order_id, correlation_id="c")
    await w.orders.request_expiry("6c7d8e9f-0a1b-4c2d-8e3f-4a5b6c7d8e9f", RAJU, "CHAIWALA")
    for _ in range(25):  # Redpanda down for 25 minutes; the relay keeps trying
        w.clock.now += timedelta(minutes=1)
        assert await w.relay.run_once() == 0
    w.publisher.down = False
    assert await w.relay.run_once() == 2
    assert w.publisher.types() == ["OrderPlaced", "OrderCancelRequested", "OrderExpiryRequested"]
    assert w.store.outbox == []


async def test_the_relay_tells_the_store_how_far_back_rows_can_still_appear(w: World) -> None:
    await w.relay.run_once()
    assert w.store.settled_before == ["2026-11-20T10:40:01.248Z"]  # now - 2 min (clock margin)


async def test_a_pending_cancel_of_an_order_that_finished_meanwhile_is_dropped(w: World) -> None:
    order = await w.place()
    w.publisher.down = True
    await w.orders.cancel(RAJU, order.order_id, correlation_id="c")
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
    w.publisher.down = False
    w.clock.now += timedelta(seconds=10)
    assert await w.relay.run_once() == 0
    assert w.publisher.types() == ["OrderPlaced"] and w.store.outbox == []


async def test_a_failing_pass_is_logged_and_the_loop_goes_on(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    async def broken() -> int:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        raise OSError("cassandra down")

    monkeypatch.setattr(w.relay, "run_once", broken)
    with pytest.raises(KeyboardInterrupt):
        await w.relay.run(every_seconds=0)
    assert calls == 2
