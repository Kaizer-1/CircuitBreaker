"""Integration tests: the market store on a real Cassandra 5 (testcontainers), and a projection
that restarts from it.

Run with `uv run pytest -m integration --no-cov` (Docker needed).
"""

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from testcontainers.community.cassandra import CassandraContainer

from market_data_service.adapters.cassandra_store import CassandraMarketStore
from market_data_service.application.projection import MarketProjection
from market_data_service.domain.candles import Candle
from market_data_service.domain.market import DayStats, Trade, utc_day
from market_data_service.ports.market_store import Changes
from tests.fakes import Clock, Exchange, FakeEventStore, RecordingPublisher

pytestmark = pytest.mark.integration
TABLES = ("candles", "trades_by_ticker", "last_prices")
T = 1_795_000_200_000


@pytest.fixture(scope="module")
def session() -> Iterator[Any]:
    from cassandra.cluster import Cluster

    with CassandraContainer("cassandra:5.0.9") as container:
        cluster = Cluster(container.get_contact_points(), protocol_version=4)
        admin = cluster.connect()
        admin.execute(
            "CREATE KEYSPACE market_data_it WITH replication = "
            "{'class': 'SimpleStrategy', 'replication_factor': 1}"
        )
        admin.set_keyspace("market_data_it")
        yield admin
        cluster.shutdown()


@pytest.fixture
async def store(session: Any) -> CassandraMarketStore:
    for table in TABLES:
        session.execute(f"DROP TABLE IF EXISTS {table}")
    s = CassandraMarketStore(session)
    await s.migrate()
    await s.migrate()  # migrations are idempotent
    return s


def trade(n: int, at: int) -> Trade:
    return Trade(f"a0b1c2d3-e4f5-4a6b-8c7d-{n:012d}", n, 24500 + 5 * n, n, "BUY", at)


async def test_a_save_is_loaded_back_exactly(store: CassandraMarketStore) -> None:
    candles = {
        ("5s", T): Candle(T, 24500, 24600, 24400, 24550, 30, 3),
        ("5s", T + 5_000): Candle(T + 5_000, 24550, 24550, 24550, 24550, 0, 0),
        ("1m", T): Candle(T, 24500, 24600, 24400, 24550, 30, 3),
        ("5m", T): Candle(T, 24500, 24600, 24400, 24550, 30, 3),
    }
    trades = [trade(1, T + 10), trade(2, T + 20), trade(3, T + 20)]
    day = DayStats(utc_day(T), 24500, 24600, 24400, 30)
    await store.save(Changes("CHAIWALA", 7, T + 30, 24515, T + 20, day, candles, trades))

    loaded = await store.load("CHAIWALA")
    assert loaded is not None
    assert (loaded.sequence, loaded.last_price_paise, loaded.last_trade_at_ms) == (7, 24515, T + 20)
    assert loaded.as_of_ms == T + 30 and loaded.day == day
    assert loaded.candles["5s"] == [candles[("5s", T)], candles[("5s", T + 5_000)]]
    assert [t.sequence for t in loaded.recent_trades] == [3, 2, 1]
    assert loaded.recent_trades[-1] == trades[0]
    assert await store.load("JUGAAD") is None


async def test_candles_and_trades_answer_q03_and_q05(store: CassandraMarketStore) -> None:
    candles = {("5s", T + i * 5_000): Candle(T + i * 5_000, 1, 1, 1, 1, 0, 0) for i in range(10)}
    trades = [trade(n, T + n) for n in range(1, 6)]
    await store.save(Changes("DABBA", 5, T, 5, T + 5, None, candles, trades))
    window = await store.candles("DABBA", "5s", T + 10_000, T + 30_000, 3)
    assert [c.bucket_start_ms for c in window] == [T + 20_000, T + 25_000, T + 30_000]
    assert [t.sequence for t in await store.trades("DABBA", 2)] == [5, 4]
    assert await store.trades("FILMY", 10) == []


async def test_a_projection_restarted_from_cassandra_shows_the_same_market(
    store: CassandraMarketStore,
) -> None:
    clock = Clock()
    exchange = Exchange(clock=clock)
    events = FakeEventStore()
    events.events["CHAIWALA"] = exchange.log
    first = MarketProjection(store, events, RecordingPublisher(), environment="dev", clock=clock)
    await first.start()
    for price in (24600, 24650, 24500):
        exchange.place("b", "SELL", 4, price)
        exchange.place("a", "BUY", 4, price)
        clock.now += timedelta(seconds=4)
    exchange.place("b", "SELL", 9, 24900)
    for e in exchange.log:
        await first.handle(e)
    clock.now += timedelta(seconds=7)
    await first.tick()

    events.snapshots["CHAIWALA"] = exchange.snapshot()
    second = MarketProjection(store, events, RecordingPublisher(), environment="dev", clock=clock)
    await second.start()
    await second.tick()
    a, b = first.markets["CHAIWALA"], second.markets["CHAIWALA"]
    assert (b.sequence, b.last_price_paise, list(b.recent)) == (
        a.sequence,
        a.last_price_paise,
        list(a.recent),
    )
    assert b.depth_levels(10) == a.depth_levels(10)
    assert a.series is not None and b.series is not None
    end = a.series["5s"].forming_start_ms
    assert b.series["5s"].candles(0, end) == a.series["5s"].candles(0, end)
