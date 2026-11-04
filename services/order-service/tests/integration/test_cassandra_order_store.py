"""Integration tests: the order store on a real Cassandra 5 (testcontainers).

Run with `uv run pytest -m integration --no-cov` (Docker needed).
"""

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from testcontainers.community.cassandra import CassandraContainer

from order_service.adapters.cassandra_store import CassandraOrderStore
from order_service.domain.order import Order, OrderStatus, OrderType, Side
from order_service.events.orders import order_placed
from order_service.ports.order_store import OutboxEntry
from order_service.runtime.events import format_timestamp

pytestmark = pytest.mark.integration
RAJU = "3d2a1b0c-9e8f-4a7b-8c6d-5e4f3a2b1c0d"
TABLES = (
    "orders_by_id",
    "orders_by_trader",
    "order_events",
    "outbox",
    "orders_by_idempotency_key",
    "processed_events",
)


@pytest.fixture(scope="module")
def session() -> Iterator[Any]:
    from cassandra.cluster import Cluster

    with CassandraContainer("cassandra:5.0.9") as container:
        cluster = Cluster(container.get_contact_points(), protocol_version=5)
        admin = cluster.connect()
        admin.execute(
            "CREATE KEYSPACE order_service_it WITH replication = "
            "{'class': 'SimpleStrategy', 'replication_factor': 1}"
        )
        admin.set_keyspace("order_service_it")
        yield admin
        cluster.shutdown()


@pytest.fixture
async def store(session: Any) -> CassandraOrderStore:
    for table in TABLES:
        session.execute(f"DROP TABLE IF EXISTS {table}")
    s = CassandraOrderStore(session)
    await s.migrate()
    return s


def order(n: int, at: datetime) -> Order:
    stamp = format_timestamp(at)
    return Order(
        order_id=f"0dde0000-0000-4000-8000-{n:012d}",
        trader_id=RAJU,
        ticker="CHAIWALA",
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
    pending = await store.pending_outbox(format_timestamp(now - timedelta(minutes=15)))
    assert [p.event.event_id for p in pending] == [entry.event.event_id]
    await store.mark_published(entry)
    assert await store.pending_outbox(format_timestamp(now - timedelta(minutes=15))) == []
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
