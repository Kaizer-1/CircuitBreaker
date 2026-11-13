"""Integration tests: the order store on a real Cassandra 5 (testcontainers).

Run with `uv run pytest -m integration --no-cov` (Docker needed).
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from order_service.adapters.cassandra_store import CassandraOrderStore, bucket_of
from order_service.domain.order import Order, OrderStatus, OrderType, Side
from order_service.events.orders import order_cancel_requested, order_placed
from order_service.ports.order_store import OutboxEntry
from order_service.runtime.events import format_timestamp

pytestmark = pytest.mark.integration
RAJU = "3d2a1b0c-9e8f-4a7b-8c6d-5e4f3a2b1c0d"


def order(n: int, at: datetime) -> Order:
    stamp = format_timestamp(at)
    return Order(
        order_id=f"0dde0000-0000-4000-8000-{n:012d}",
        trader_id=RAJU,
        ticker="CALVERA",
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=50,
        limit_price_paise=24600,
        protection_price_paise=None,
        reservation_id="c1d2e3f4-a5b6-4c7d-8e9f-0a1b2c3d4e5f",
        status=OrderStatus.PENDING,
        placed_at=stamp,
        updated_at=stamp,
    )


async def test_an_order_round_trips_and_its_outbox_row_is_pending_until_published(
    store: CassandraOrderStore,
) -> None:
    now = datetime.now(UTC)
    o = order(1, now)
    entry = OutboxEntry(
        o.order_id, o.ticker, order_placed(o, now, environment="dev", correlation_id="c")
    )
    await store.create(o, entry)
    assert await store.get(o.order_id) == o
    pending = await store.pending_outbox(format_timestamp(now - timedelta(minutes=2)))
    assert [p.event.event_id for p in pending] == [entry.event.event_id]
    await store.mark_published(entry)
    assert await store.pending_outbox(format_timestamp(now - timedelta(minutes=2))) == []
    stored = await store.get(o.order_id)
    assert stored is not None and stored.published
    assert (await store.find_event(o.order_id, "OrderPlaced")) == entry.event


async def test_status_writes_never_clobber_a_cancel_request(store: CassandraOrderStore) -> None:
    now = datetime.now(UTC)
    o = order(2, now)
    await store.create(o, None)
    stale = replace(o)  # the consumer read the order before the trader cancelled
    cancel = OutboxEntry(
        o.order_id, o.ticker, order_placed(o, now, environment="dev", correlation_id="x")
    )
    await store.set_cancel_requested(o.order_id, format_timestamp(now), cancel)
    stale.accepted(1041, format_timestamp(now))
    await store.save_status([stale], processed_event_id="e-1", cancel_flag=False)
    after = await store.get(o.order_id)
    assert after is not None and (after.status, after.cancel_requested) == (OrderStatus.OPEN, True)
    assert await store.is_processed("e-1")


async def test_an_idempotency_key_is_claimed_once(store: CassandraOrderStore) -> None:
    assert await store.claim_idempotency_key(RAJU, "phone-retry-1", "order-a") == "order-a"
    assert await store.claim_idempotency_key(RAJU, "phone-retry-1", "order-b") == "order-a"
    assert (
        await store.claim_idempotency_key("someone-else", "phone-retry-1", "order-c") == "order-c"
    )


async def test_a_traders_orders_page_newest_first(store: CassandraOrderStore) -> None:
    start = datetime.now(UTC)
    for n in range(1, 6):
        await store.create(order(10 + n, start + timedelta(seconds=n)), None)
    first = await store.orders_of(RAJU, before=None, limit=2)
    assert [o.order_id[-2:] for o in first] == ["15", "14"]
    rest = await store.orders_of(RAJU, before=(first[-1].placed_at, first[-1].order_id), limit=10)
    assert [o.order_id[-2:] for o in rest] == ["13", "12", "11"]


async def test_a_dropped_outbox_row_is_never_seen_again(store: CassandraOrderStore) -> None:
    now = datetime.now(UTC)
    o = order(3, now)
    entry = OutboxEntry(
        o.order_id, o.ticker, order_placed(o, now, environment="dev", correlation_id="c")
    )
    await store.create(o, entry)
    await store.drop_outbox(entry)
    assert await store.pending_outbox(format_timestamp(now - timedelta(minutes=1))) == []


def placed(n: int, at: datetime) -> tuple[Order, OutboxEntry]:
    o = order(n, at)
    return o, OutboxEntry(
        o.order_id, o.ticker, order_placed(o, at, environment="dev", correlation_id="c")
    )


async def test_a_row_of_any_age_is_found_and_the_cursor_never_passes_it(
    store: CassandraOrderStore, session: Any
) -> None:
    # Phase 4: the relay used to look back 15 minutes, so a cancel older than that was never sent.
    now = datetime.now(UTC)
    settled = format_timestamp(now - timedelta(minutes=2))
    older, oldest = placed(20, now - timedelta(minutes=25)), placed(21, now - timedelta(minutes=40))
    for o, entry in (older, oldest):
        await store.create(o, entry)

    # The first pass ever (no cursor yet) starts at the oldest row in the outbox.
    found = await store.pending_outbox(settled)
    assert [e.order_id for e in found] == [oldest[0].order_id, older[0].order_id]
    await store.mark_published(oldest[1])
    assert [e.order_id for e in await store.pending_outbox(settled)] == [older[0].order_id]
    cursor = session.execute("SELECT bucket FROM outbox_cursor WHERE name = 'relay'").one()
    assert cursor.bucket == bucket_of(now - timedelta(minutes=25))  # held at the waiting row

    # A restarted service starts at the saved cursor and still finds the 25-minute-old row.
    restarted = CassandraOrderStore(session)
    await restarted.migrate()
    assert [e.order_id for e in await restarted.pending_outbox(settled)] == [older[0].order_id]
    await restarted.mark_published(older[1])
    assert await restarted.pending_outbox(settled) == []
    cursor = session.execute("SELECT bucket FROM outbox_cursor WHERE name = 'relay'").one()
    assert cursor.bucket == bucket_of(now - timedelta(minutes=2))  # past the empty minutes


async def test_a_requeued_request_is_a_fresh_row_of_the_same_event(
    store: CassandraOrderStore,
) -> None:
    now = datetime.now(UTC)
    o, entry = placed(22, now - timedelta(minutes=30))
    await store.create(o, entry)
    await store.mark_published(entry)
    first = order_cancel_requested(
        o, now - timedelta(minutes=29), environment="dev", correlation_id="c"
    )
    second = order_cancel_requested(
        o, now - timedelta(minutes=10), environment="dev", correlation_id="c"
    )
    for at, event in ((first.occurred_at, first), (second.occurred_at, second)):
        await store.set_cancel_requested(o.order_id, at, OutboxEntry(o.order_id, o.ticker, event))
        await store.mark_published(OutboxEntry(o.order_id, o.ticker, event))
    assert await store.find_event(o.order_id, "OrderCancelRequested") == second  # the newest

    again = OutboxEntry(o.order_id, o.ticker, second, written_at=format_timestamp(now))
    await store.requeue(again)
    [pending] = await store.pending_outbox(format_timestamp(now - timedelta(minutes=2)))
    assert pending == again
    await store.mark_published(pending)
    assert await store.pending_outbox(format_timestamp(now - timedelta(minutes=2))) == []
