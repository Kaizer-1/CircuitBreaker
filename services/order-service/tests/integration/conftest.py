"""Containers shared by the integration tests (one of each per run)."""

import socket
from collections.abc import Iterator
from typing import Any

import pytest
from testcontainers.community.cassandra import CassandraContainer
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from order_service.adapters.cassandra_store import CassandraOrderStore

TABLES = (
    "orders_by_id",
    "orders_by_trader",
    "order_events",
    "outbox",
    "outbox_cursor",
    "orders_by_idempotency_key",
    "processed_events",
)


@pytest.fixture(scope="session")
def session() -> Iterator[Any]:
    from cassandra.cluster import Cluster

    with CassandraContainer("cassandra:5.0.9") as container:
        cluster = Cluster(container.get_contact_points(), protocol_version=4)
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
    """A fresh, empty keyspace for each test."""
    for table in TABLES:
        session.execute(f"DROP TABLE IF EXISTS {table}")
    s = CassandraOrderStore(session)
    await s.migrate()
    return s


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def redpanda() -> Iterator[tuple[DockerContainer, str]]:
    """Redpanda on a fixed host port, so it is at the same address after a stop and start."""
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
