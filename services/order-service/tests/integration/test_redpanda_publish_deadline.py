"""Integration tests: the 60 s publish rule on a real Redpanda (testcontainers).

The relay never *starts* sending an OrderPlaced that is older than 60 s - D, and the Kafka client
gives up on a message D seconds after it was handed over (ADR-0017, "A publish that hung"). So no
OrderPlaced reaches the broker more than 60 s after it was placed. These tests prove the second
half against a real broker that is stopped and started again, and show why it needed a change:
an idempotent aiokafka producer never gives up on a message, so it delivers it whenever the broker
comes back.

Redpanda listens on a fixed host port so that it is reachable at the same address after a restart.
Run with `uv run pytest -m integration --no-cov` (Docker needed).
"""

import asyncio
import socket
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from order_service.runtime.events import EventEnvelope, build_event
from order_service.runtime.kafka import EventPublisher
from order_service.runtime.metrics import Metrics

pytestmark = pytest.mark.integration
TOPIC = "orders.v1"
DELIVERY_TIMEOUT = 3.0  # the production value is 10 s; 3 s keeps the test short


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def redpanda() -> Iterator[tuple[DockerContainer, str]]:
    port = free_port()
    container = (
        DockerContainer("redpandadata/redpanda:v26.2.6")
        .with_bind_ports(port, port)
        .with_command(
            "redpanda start --mode dev-container --smp 1 --memory 512M --overprovisioned "
            f"--kafka-addr PLAINTEXT://0.0.0.0:{port} "
            f"--advertise-kafka-addr PLAINTEXT://127.0.0.1:{port}"
        )
        .waiting_for(LogMessageWaitStrategy("Successfully started Redpanda"))
    )
    with container:
        yield container, f"127.0.0.1:{port}"


def rpk(container: DockerContainer, bootstrap: str, *args: str) -> str:
    port = bootstrap.rsplit(":", 1)[1]
    code, out = container.exec(["rpk", "-X", f"brokers=127.0.0.1:{port}", *args])
    assert code == 0, out
    return out.decode() if isinstance(out, bytes) else str(out)


@pytest.fixture
async def topic(redpanda: tuple[DockerContainer, str]) -> AsyncIterator[str]:
    container, bootstrap = redpanda
    rpk(container, bootstrap, "topic", "create", TOPIC, "-p", "1")
    yield TOPIC
    rpk(container, bootstrap, "topic", "delete", TOPIC)


def message(label: str) -> EventEnvelope:
    return build_event(
        "OrderPlaced",
        {"label": label},
        producer="order-service",
        environment="dev",
        correlation_id=label,
    )


async def wait_until_up(container: DockerContainer, bootstrap: str) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        code, _ = container.exec(
            ["rpk", "-X", f"brokers={bootstrap}", "cluster", "health", "--exit-when-healthy"]
        )
        if code == 0:
            return
        await asyncio.sleep(0.5)
    raise AssertionError("Redpanda did not come back")


async def labels_on(bootstrap: str, topic: str) -> list[str]:
    """Every message on the topic, read from the beginning by a fresh consumer."""
    from aiokafka import AIOKafkaConsumer

    consumer = AIOKafkaConsumer(
        topic, bootstrap_servers=bootstrap, auto_offset_reset="earliest", group_id=None
    )
    await consumer.start()
    try:
        found: list[str] = []
        batches = await consumer.getmany(timeout_ms=3000)
        for records in batches.values():
            found += [EventEnvelope.from_json(r.value).data["label"] for r in records]
        return found
    finally:
        await consumer.stop()


async def send_while_the_broker_is_stopped(
    redpanda: tuple[DockerContainer, str], topic: str, publisher: EventPublisher
) -> list[str]:
    """before → (broker stops) → lost, abandoned after 1 s like the relay does → (broker returns
    after D + 2 s) → after. Returns what ended up on the topic."""
    container, bootstrap = redpanda
    await publisher.publish(topic, "CHAIWALA", message("before"))  # leader now known

    docker = container.get_wrapped_container()
    docker.stop(timeout=1)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(publisher.publish(topic, "CHAIWALA", message("lost")), 1.0)
    await asyncio.sleep(DELIVERY_TIMEOUT + 2)  # past the client's deadline, then the broker returns
    docker.start()
    await wait_until_up(container, bootstrap)

    await asyncio.wait_for(publisher.publish(topic, "CHAIWALA", message("after")), 30)
    await asyncio.sleep(1)  # anything the client still held has had time to arrive
    return await labels_on(bootstrap, topic)


async def test_a_bounded_publisher_never_delivers_a_message_after_its_deadline(
    redpanda: tuple[DockerContainer, str], topic: str
) -> None:
    publisher = EventPublisher(
        redpanda[1],
        "order-service",
        Metrics("order-service"),
        delivery_timeout_seconds=DELIVERY_TIMEOUT,
    )
    await publisher.start()
    try:
        assert await send_while_the_broker_is_stopped(redpanda, topic, publisher) == [
            "before",
            "after",
        ]
    finally:
        await publisher.stop()


async def test_a_bounded_send_gives_up_within_its_deadline_while_the_broker_is_down(
    redpanda: tuple[DockerContainer, str], topic: str
) -> None:
    from aiokafka.errors import KafkaError

    container, bootstrap = redpanda
    publisher = EventPublisher(
        bootstrap,
        "order-service",
        Metrics("order-service"),
        delivery_timeout_seconds=DELIVERY_TIMEOUT,
    )
    await publisher.start()
    docker = container.get_wrapped_container()
    try:
        await publisher.publish(topic, "CHAIWALA", message("before"))
        docker.stop(timeout=1)
        started = time.monotonic()
        with pytest.raises(KafkaError):
            await asyncio.wait_for(publisher.publish(topic, "CHAIWALA", message("lost")), 30)
        assert time.monotonic() - started <= DELIVERY_TIMEOUT + 0.5
    finally:
        docker.start()
        await wait_until_up(container, bootstrap)
        await publisher.stop()


async def test_an_idempotent_publisher_delivers_a_hung_message_late(
    redpanda: tuple[DockerContainer, str], topic: str
) -> None:
    # Why the change was needed: with enable_idempotence=True aiokafka never expires a batch, so the
    # message the relay gave up on is delivered when the broker returns, however late that is.
    publisher = EventPublisher(redpanda[1], "order-service", Metrics("order-service"))
    await publisher.start()
    try:
        got: Any = await send_while_the_broker_is_stopped(redpanda, topic, publisher)
        assert got == ["before", "lost", "after"]
    finally:
        await publisher.stop()
