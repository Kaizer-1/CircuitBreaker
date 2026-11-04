"""Integration tests: the Cassandra event store against a real Cassandra (testcontainers).

Run with `uv run pytest -m integration` (Docker needed). Not part of the default `pytest` run.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from testcontainers.community.cassandra import CassandraContainer

from matching_engine.adapters.cassandra_store import CassandraEventStore
from matching_engine.application.engine import MatchingEngine
from matching_engine.runtime.events import build_event
from tests.builders import ALICE, BOB, CARA, cancel, expire, id_source, limit, market
from tests.fakes import RecordingPublisher

pytestmark = pytest.mark.integration

DAY1 = datetime(2026, 11, 20, 23, 59, 58, tzinfo=UTC)


@pytest.fixture(scope="module")
def session() -> Iterator[Any]:
    from cassandra.cluster import Cluster

    with CassandraContainer("cassandra:5.0.9") as container:
        cluster = Cluster(
            container.get_contact_points(),
            load_balancing_policy=None,
            protocol_version=4,
        )
        admin = cluster.connect()
        admin.execute(
            "CREATE KEYSPACE matching_engine_it WITH replication = "
            "{'class': 'SimpleStrategy', 'replication_factor': 1}"
        )
        admin.set_keyspace("matching_engine_it")
        yield admin
        cluster.shutdown()


@pytest.fixture
async def store(session: Any) -> CassandraEventStore:
    for table in (
        "market_events",
        "ticker_days",
        "book_snapshots",
        "processed_commands",
        "publish_cursor",
    ):
        session.execute(f"DROP TABLE IF EXISTS {table}")
    s = CassandraEventStore(session)
    await s.migrate()
    return s


def event(sequence: int, at: datetime, ticker: str = "CHAIWALA") -> Any:
    return build_event(
        "OrderExpired",
        {
            "sequence": sequence,
            "ticker": ticker,
            "order_id": f"0dde0000-0000-4000-8000-{sequence:012d}",
            "trader_id": ALICE,
            "expired_at": at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        },
        producer="matching-engine",
        environment="dev",
        correlation_id="it",
        occurred_at=at,
    )


async def test_events_are_read_back_in_sequence_across_day_partitions(
    store: CassandraEventStore,
) -> None:
    for n in range(1, 7):  # three on day 1, three on day 2: two (ticker, day) partitions
        await store.append("CHAIWALA", [event(n, DAY1 + timedelta(seconds=n - 1))], f"cmd-{n}")
    got = await store.events_after("CHAIWALA", 0)
    assert [e.data["sequence"] for e in got] == [1, 2, 3, 4, 5, 6]
    assert [e.data["sequence"] for e in await store.events_after("CHAIWALA", 4)] == [5, 6]
    assert await store.events_after("JUGAAD", 0) == []


async def test_events_after_honours_limit_and_until(store: CassandraEventStore) -> None:
    for n in range(1, 6):
        await store.append("CHAIWALA", [event(n, DAY1 + timedelta(seconds=n))], f"cmd-{n}")
    page = await store.events_after("CHAIWALA", 1, limit=2)
    assert [e.data["sequence"] for e in page] == [2, 3]
    until = (DAY1 + timedelta(seconds=3)).isoformat().replace("+00:00", "Z")
    assert [e.data["sequence"] for e in await store.events_after("CHAIWALA", 0, until=until)] == [
        1,
        2,
        3,
    ]


async def test_the_command_id_is_recorded_with_its_events(store: CassandraEventStore) -> None:
    assert await store.is_processed("cmd-1") is False
    await store.append("CHAIWALA", [event(1, DAY1)], "cmd-1")
    assert await store.is_processed("cmd-1") is True
    await store.append("CHAIWALA", [], "cmd-refused")  # a refused command: no events, but processed
    assert await store.is_processed("cmd-refused") is True


async def test_the_latest_snapshot_at_or_before_a_time(store: CassandraEventStore) -> None:
    def snap(sequence: int, at: datetime) -> dict[str, Any]:
        stamp = at.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        return {
            "ticker": "CHAIWALA",
            "sequence": sequence,
            "as_of": stamp,
            "bids": [],
            "asks": [],
            "closed": [],
        }

    assert await store.latest_snapshot("CHAIWALA") is None
    await store.save_snapshot("CHAIWALA", snap(500, DAY1))
    await store.save_snapshot("CHAIWALA", snap(1000, DAY1 + timedelta(minutes=5)))
    latest = await store.latest_snapshot("CHAIWALA")
    assert latest is not None and latest["sequence"] == 1000
    at = (DAY1 + timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    earlier = await store.latest_snapshot("CHAIWALA", at)
    assert earlier is not None and earlier["sequence"] == 500


async def test_the_publish_cursor_round_trips(store: CassandraEventStore) -> None:
    assert await store.publish_cursor("CHAIWALA") == 0
    await store.set_publish_cursor("CHAIWALA", 42)
    assert await store.publish_cursor("CHAIWALA") == 42


async def test_an_engine_restarted_against_real_cassandra_rebuilds_the_same_books(
    store: CassandraEventStore,
) -> None:
    def make() -> MatchingEngine:
        return MatchingEngine(
            store, RecordingPublisher(), environment="dev", snapshot_every=3, new_id=id_source()
        )

    engine = make()
    await engine.recover()
    for command in [
        limit(1, BOB, "SELL", 20, 24550),
        limit(2, CARA, "SELL", 10, 24560),
        limit(3, ALICE, "BUY", 25, 24600),
        market(4, ALICE, "BUY", 50, 27005),
        limit(5, BOB, "BUY", 7, 24400),
        cancel(5, BOB),
        expire(6, ALICE),
    ]:
        await engine.handle(command)
    before = engine.books_hash()
    restarted = make()
    await restarted.recover()
    assert restarted.books_hash() == before
