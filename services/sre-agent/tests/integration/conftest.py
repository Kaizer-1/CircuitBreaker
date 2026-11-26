"""A real Cassandra (5.0.9, like the cluster) for the store's integration tests."""

from collections.abc import Iterator
from typing import Any

import pytest
from testcontainers.community.cassandra import CassandraContainer

from sre_agent.adapters.cassandra_store import CassandraStore

TABLES = ("incidents", "incidents_by_day", "agent_settings", "action_log", "incident_counter")


@pytest.fixture(scope="session")
def session() -> Iterator[Any]:
    from cassandra.cluster import Cluster

    with CassandraContainer("cassandra:5.0.9") as container:
        cluster = Cluster(container.get_contact_points(), protocol_version=4)
        admin = cluster.connect()
        admin.execute(
            "CREATE KEYSPACE sre_agent_it WITH replication = "
            "{'class': 'SimpleStrategy', 'replication_factor': 1}"
        )
        admin.set_keyspace("sre_agent_it")
        yield admin
        cluster.shutdown()


@pytest.fixture
async def store(session: Any) -> CassandraStore:
    for table in TABLES:
        session.execute(f"DROP TABLE IF EXISTS {table}")
    s = CassandraStore(session)
    await s.migrate()
    return s
