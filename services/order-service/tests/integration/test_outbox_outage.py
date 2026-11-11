"""Integration test: a cancel and an expiry request survive a 25-minute Redpanda outage.

Real Cassandra (the outbox and its cursor) and a real Redpanda that is stopped while the trader
cancels and portfolio-service asks for an expiry. The service's clock runs 25 minutes on during the
outage (the relay keeps trying, minute by minute); then Redpanda returns and both requests must be
on orders.v1. Until Phase 4 the relay looked back only 15 minutes, so neither was ever sent: the
order rested for ever and the money stayed reserved (ADR-0017 note "No time limit for cancels and
expiries"). Run with `uv run pytest -m integration --no-cov` (Docker needed).
"""

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from testcontainers.core.container import DockerContainer

from order_service.adapters.cassandra_store import CassandraOrderStore
from order_service.application.orders import Orders
from order_service.application.outbox_relay import OutboxRelay
from order_service.domain.order import OrderRequest, OrderType, Side
from order_service.runtime.events import EventEnvelope
from order_service.runtime.kafka import EventPublisher
from order_service.runtime.metrics import Metrics
from tests.fakes import FakePortfolio
from tests.integration.test_redpanda_publish_deadline import rpk, wait_until_up

pytestmark = pytest.mark.integration
TOPIC = "orders.v1"
RAJU = "3d2a1b0c-9e8f-4a7b-8c6d-5e4f3a2b1c0d"
ORPHAN = "6c7d8e9f-0a1b-4c2d-8e3f-4a5b6c7d8e9f"  # reserved, then order-service lost it


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
async def publisher(redpanda: tuple[DockerContainer, str]) -> AsyncIterator[EventPublisher]:
    container, bootstrap = redpanda
    rpk(container, bootstrap, "topic", "create", TOPIC, "-p", "1")
    publisher = EventPublisher(
        bootstrap, "order-service", Metrics("order-service"), delivery_timeout_seconds=3.0
    )
    await publisher.start()
    yield publisher
    await publisher.stop()
    rpk(container, bootstrap, "topic", "delete", TOPIC)


async def on_topic(bootstrap: str) -> list[EventEnvelope]:
    from aiokafka import AIOKafkaConsumer

    consumer = AIOKafkaConsumer(
        TOPIC, bootstrap_servers=bootstrap, auto_offset_reset="earliest", group_id=None
    )
    await consumer.start()
    try:
        batches = await consumer.getmany(timeout_ms=3000)
        return [EventEnvelope.from_json(r.value) for rs in batches.values() for r in rs]
    finally:
        await consumer.stop()


async def test_a_cancel_and_an_expiry_are_delivered_after_a_25_minute_outage(
    store: CassandraOrderStore,
    publisher: EventPublisher,
    redpanda: tuple[DockerContainer, str],
) -> None:
    container, bootstrap = redpanda
    clock = Clock(datetime.now(UTC) - timedelta(minutes=30))
    orders = Orders(store, FakePortfolio(), publisher, environment="dev", clock=clock)
    relay = OutboxRelay(store, publisher, clock=clock, attempt_seconds=0.5)
    request = OrderRequest("CHAIWALA", Side.BUY, OrderType.LIMIT, 50, 24600)
    order = await orders.place(RAJU, request, correlation_id="c")
    assert (await store.get(order.order_id)).published  # type: ignore[union-attr]

    docker = container.get_wrapped_container()
    docker.stop(timeout=1)
    try:
        clock.now += timedelta(seconds=30)
        await orders.cancel(RAJU, order.order_id, correlation_id="c")  # the trader cancels
        await orders.request_expiry(ORPHAN, RAJU, "CHAIWALA")  # the sweeper asks
        for _ in range(25):  # 25 minutes of outage, the relay trying every minute
            clock.now += timedelta(minutes=1)
            assert await relay.run_once() == 0
    finally:
        docker.start()
        await wait_until_up(container, bootstrap)

    clock.now += timedelta(seconds=1)
    deadline = time.monotonic() + 60
    while len(await store.pending_outbox(clock.now.isoformat())) > 0:
        assert time.monotonic() < deadline, "the relay did not deliver after the outage"
        await relay.run_once()
        await asyncio.sleep(0.5)

    sent = {(e.event_type, str(e.data["order_id"])) for e in await on_topic(bootstrap)}
    assert ("OrderCancelRequested", order.order_id) in sent
    assert ("OrderExpiryRequested", ORPHAN) in sent
