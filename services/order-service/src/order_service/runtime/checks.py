"""Readiness probes for the three backing services.

Drivers are imported inside the probes so a service only needs the drivers it uses (the same file is
copied into every service).
"""

import asyncio
import threading
from collections.abc import Sequence
from typing import Any

from order_service.runtime.health import ReadinessCheck


def redpanda_check(bootstrap_servers: str, topics: Sequence[str] = ()) -> ReadinessCheck:
    """Redpanda is reachable and the topics this service uses exist (made by the init job)."""

    async def probe() -> None:
        from aiokafka.client import AIOKafkaClient

        client = AIOKafkaClient(
            bootstrap_servers=bootstrap_servers, request_timeout_ms=3000, client_id="readiness"
        )
        try:
            await client.bootstrap()
            metadata = await client.fetch_all_metadata()
            missing = sorted(set(topics) - set(metadata.topics()))
            if missing:
                raise RuntimeError(f"topics missing: {', '.join(missing)}")
        finally:
            await client.close()

    return ReadinessCheck("redpanda", probe)


def cassandra_check(
    contact_points: Sequence[str], port: int, keyspace: str, username: str, password: str
) -> ReadinessCheck:
    """Can log in with this service's own role and use its own keyspace."""
    state: dict[str, Any] = {}
    lock = threading.Lock()

    def probe_sync() -> None:
        from cassandra.auth import PlainTextAuthProvider
        from cassandra.cluster import Cluster
        from cassandra.io.asyncioreactor import AsyncioConnection

        with lock:
            if "session" not in state:
                cluster = Cluster(
                    list(contact_points),
                    port=port,
                    auth_provider=PlainTextAuthProvider(username, password),
                    connection_class=AsyncioConnection,
                    connect_timeout=3,
                    protocol_version=4,  # as the adapters: v5 framing failed under load
                )
                try:
                    state["session"] = cluster.connect(
                        keyspace
                    )  # fails if the keyspace/role is missing
                except Exception:
                    cluster.shutdown()
                    raise
                state["cluster"] = cluster
            try:
                state["session"].execute("SELECT release_version FROM system.local", timeout=3)
            except Exception:
                state["cluster"].shutdown()
                state.clear()
                raise

    async def probe() -> None:
        await asyncio.to_thread(probe_sync)

    return ReadinessCheck("cassandra", probe)


def postgres_check(host: str, port: int, dbname: str, user: str, password: str) -> ReadinessCheck:
    """Can log in with this service's own role and run a query."""

    async def probe() -> None:
        import psycopg

        conn = await psycopg.AsyncConnection.connect(
            host=host, port=port, dbname=dbname, user=user, password=password, connect_timeout=3
        )
        async with conn:
            await conn.execute("SELECT 1")

    return ReadinessCheck("postgres", probe)
