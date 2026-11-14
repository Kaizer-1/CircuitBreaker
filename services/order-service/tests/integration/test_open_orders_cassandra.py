"""Integration: the open-orders table on a real Cassandra (Q-07 status=open).

The property of tests/property/test_open_orders.py, played on the real store: after every step the
table holds exactly the orders that are not finished. Then the one-time backfill: open orders
stored before the table existed join it, and an order that closes while the backfill runs does not
come back (each row is written with its order's status timestamp). Run with `uv run pytest -m
integration --no-cov` (Docker needed).
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings

from order_service.adapters.cassandra_store import CassandraOrderStore
from order_service.domain.order import Order, OrderStatus, OrderType, Side
from order_service.runtime.events import format_timestamp
from tests.property.open_orders_workload import run, workloads

pytestmark = pytest.mark.integration


def test_the_open_orders_table_equals_the_orders_not_finished(session: Any) -> None:
    store = CassandraOrderStore(session)  # one keyspace; each workload has its own traders
    asyncio.run(store.migrate())

    @settings(max_examples=40, suppress_health_check=[HealthCheck.too_slow])
    @given(workloads)
    def check(workload: list[tuple[Any, ...]]) -> None:
        asyncio.run(run(store, workload))

    check()


def order(n: int, trader: str, status: OrderStatus) -> Order:
    at = format_timestamp(datetime(2026, 11, 14, 9, 0, n, tzinfo=UTC))
    return Order(
        order_id=f"0bac0000-0000-4000-8000-{n:012d}",
        trader_id=trader,
        ticker="CALVERA",
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=10,
        limit_price_paise=24600,
        protection_price_paise=None,
        reservation_id=None,
        status=status,
        placed_at=at,
        updated_at=at,
    )


async def test_the_backfill_adds_stored_open_orders_and_never_a_closed_one(
    store: CassandraOrderStore, session: Any
) -> None:
    trader = "3d2a1b0c-9e8f-4a7b-8c6d-5e4f3a2b1c0d"
    resting, filled, closing = (
        order(1, trader, OrderStatus.OPEN),
        order(2, trader, OrderStatus.FILLED),
        order(3, trader, OrderStatus.PARTIALLY_FILLED),
    )
    for o in (resting, filled, closing):
        await store.create(o, None)
    # As if they were stored before the table existed: empty it and forget the backfill.
    session.execute("TRUNCATE open_orders_by_trader")
    session.execute("TRUNCATE data_migrations")
    # `closing` closes while the backfill runs: its status write lands first, then the backfill
    # writes the row it read before the close, with the older status timestamp.
    stale = session.execute(
        "SELECT WRITETIME(status) AS written FROM orders_by_id WHERE order_id = %s",
        (closing.order_id,),
    ).one()
    closing.status = OrderStatus.CANCELLED
    await store.save_status([closing], processed_event_id=None, cancel_flag=False)
    session.execute(
        "INSERT INTO open_orders_by_trader (trader_id, placed_at, order_id) VALUES (%s, %s, %s)"
        " USING TIMESTAMP %s",
        (trader, datetime(2026, 11, 14, 9, 0, 3, tzinfo=UTC), closing.order_id, stale.written),
    )

    await store.migrate()  # runs the backfill
    listed = await store.open_orders_of(trader, before=None, limit=10)
    assert [o.order_id for o in listed] == [resting.order_id]
    marker = session.execute("SELECT name FROM data_migrations").one()
    assert marker.name == "open_orders_backfill"
