"""Integration tests: the Cassandra event store against a real Cassandra (testcontainers).

Run with `uv run pytest -m integration` (Docker needed). Not part of the default `pytest` run.
"""

import asyncio
import random
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

import pytest
from testcontainers.community.cassandra import CassandraContainer

from matching_engine.adapters.cassandra_store import CassandraEventStore
from matching_engine.application.engine import MatchingEngine
from matching_engine.domain.book import OrderBook
from matching_engine.domain.model import BookSnapshot, ms_of
from matching_engine.domain.retention import HOUR_MS, hour_of, snapshots_to_keep
from matching_engine.events.market import from_envelope
from matching_engine.runtime.events import build_event
from tests.builders import ALICE, BOB, CARA, cancel, expire, id_source, limit, market
from tests.fakes import RecordingPublisher

pytestmark = pytest.mark.integration

DAY1 = datetime(2026, 11, 20, 23, 59, 58, tzinfo=UTC)
STARTED = "2026-11-20T22:59:58.000Z"  # DAY1 - 1 hour: the load test's clock starts here


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
        "book_snapshots_by_hour",
        "snapshot_hours",
        "processed_commands",
        "publish_cursor",
    ):
        session.execute(f"DROP TABLE IF EXISTS {table}")
    s = CassandraEventStore(session)
    await s.migrate()
    return s


def event(sequence: int, at: datetime, ticker: str = "CALVERA") -> Any:
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
        await store.append("CALVERA", [event(n, DAY1 + timedelta(seconds=n - 1))], f"cmd-{n}")
    got = await store.events_after("CALVERA", 0)
    assert [e.data["sequence"] for e in got] == [1, 2, 3, 4, 5, 6]
    assert [e.data["sequence"] for e in await store.events_after("CALVERA", 4)] == [5, 6]
    assert await store.events_after("KORVIN", 0) == []


async def test_events_after_honours_limit_and_until(store: CassandraEventStore) -> None:
    for n in range(1, 6):
        await store.append("CALVERA", [event(n, DAY1 + timedelta(seconds=n))], f"cmd-{n}")
    page = await store.events_after("CALVERA", 1, limit=2)
    assert [e.data["sequence"] for e in page] == [2, 3]
    until = (DAY1 + timedelta(seconds=3)).isoformat().replace("+00:00", "Z")
    assert [e.data["sequence"] for e in await store.events_after("CALVERA", 0, until=until)] == [
        1,
        2,
        3,
    ]


async def test_the_command_id_is_recorded_with_its_events(store: CassandraEventStore) -> None:
    assert await store.is_processed("cmd-1") is False
    await store.append("CALVERA", [event(1, DAY1)], "cmd-1")
    assert await store.is_processed("cmd-1") is True
    await store.append("CALVERA", [], "cmd-refused")  # a refused command: no events, but processed
    assert await store.is_processed("cmd-refused") is True


async def test_the_latest_snapshot_at_or_before_a_time(store: CassandraEventStore) -> None:
    def snap(sequence: int, at: datetime) -> dict[str, Any]:
        stamp = at.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        return {
            "ticker": "CALVERA",
            "sequence": sequence,
            "as_of": stamp,
            "bids": [],
            "asks": [],
            "closed": [],
        }

    assert await store.latest_snapshot("CALVERA") is None
    await store.save_snapshot("CALVERA", snap(500, DAY1))
    await store.save_snapshot("CALVERA", snap(1000, DAY1 + timedelta(minutes=5)))
    latest = await store.latest_snapshot("CALVERA")
    assert latest is not None and latest["sequence"] == 1000
    at = (DAY1 + timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    earlier = await store.latest_snapshot("CALVERA", at)
    assert earlier is not None and earlier["sequence"] == 500


async def test_the_publish_cursor_round_trips(store: CassandraEventStore) -> None:
    assert await store.publish_cursor("CALVERA") == 0
    await store.set_publish_cursor("CALVERA", 42)
    assert await store.publish_cursor("CALVERA") == 42


async def test_an_engine_restarted_against_real_cassandra_rebuilds_the_same_books(
    store: CassandraEventStore,
) -> None:
    def make() -> MatchingEngine:
        return MatchingEngine(
            store,
            RecordingPublisher(),
            environment="dev",
            snapshot_every=3,
            snapshot_spacing_ms=0,  # milliseconds apart: P-03's spacing would allow just one
            new_id=id_source(),
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


def stamp(at: datetime) -> str:
    return at.isoformat(timespec="milliseconds").replace("+00:00", "Z")


async def test_reads_under_load_return_every_event_once_in_order_and_rebuild_exactly(
    store: CassandraEventStore,
) -> None:
    """Rows are parsed in a worker thread (Phase 4): the order must still be the store's exactly.

    The engine appends a random workload on two tickers while eight readers page through Q-14's
    store call at the same time. Every reader must see sequences 1, 2, 3 ... with no gap, no repeat
    and no swap, and the same events the engine published, in the same order. Then the engine's way
    of rebuilding a past moment (nearest snapshot at or before T, plus the events up to T) must give
    the hash the live book had at T, for many T.
    """
    rng = random.Random(4)
    tickers = ("CALVERA", "KORVIN")
    clock = iter(DAY1 - timedelta(hours=1) + timedelta(milliseconds=10 * n) for n in count())
    publisher = RecordingPublisher()
    engine = MatchingEngine(
        store,
        publisher,
        environment="dev",
        snapshot_every=40,
        snapshot_spacing_ms=0,  # 10 ms per command: P-03's spacing would allow just one
        clock=lambda: next(clock),
        new_id=id_source(),
    )
    await engine.recover()
    traders = (ALICE, BOB, CARA)
    resting: list[tuple[int, str, str]] = []
    history: dict[str, list[tuple[str, str]]] = {
        t: [] for t in tickers
    }  # (as_of, hash) per command
    appending = True

    async def append_workload() -> None:
        nonlocal appending
        for n in range(1, 701):
            ticker = rng.choice(tickers)
            if resting and rng.random() < 0.2:
                number, trader, on = resting.pop(rng.randrange(len(resting)))
                command: Any = cancel(number, trader, ticker=on)
                ticker = on
            else:
                trader, side = rng.choice(traders), rng.choice(("BUY", "SELL"))
                price = 24500 + 5 * rng.randint(-8, 8)
                # Placed when the engine's clock starts: the engine refuses placements older than
                # its memory of one hour (forget_before), and T0 is half a day before this clock.
                command = limit(
                    n, trader, side, rng.randint(1, 30), price, ticker=ticker, placed_at=STARTED
                )
                resting.append((n, trader, ticker))
            await engine.handle(command)
            book = engine.book(ticker)
            if book.as_of is not None:
                history[ticker].append((book.as_of, book.state_hash()))
            await asyncio.sleep(0)  # let the readers in between commands
        appending = False

    async def read_all(ticker: str, page: int) -> list[str]:
        seen: list[str] = []
        after = 0
        while True:
            batch = await store.events_after(ticker, after, limit=page)
            sequences = [e.data["sequence"] for e in batch]
            assert sequences == list(range(after + 1, after + 1 + len(batch))), sequences
            seen += [e.event_id for e in batch]
            if batch:
                after = sequences[-1]
            elif not appending:
                return seen
            else:
                await asyncio.sleep(0.005)

    readers = [read_all(tickers[i % 2], page) for i, page in enumerate((1, 7, 50, 500) * 2)]
    _, *results = await asyncio.gather(append_workload(), *readers)
    for i, seen in enumerate(results):
        published = [e.event_id for e in publisher.first_copies(tickers[i % 2])]
        assert seen == published

    # Rebuild at T = snapshot at or before T + events up to T, as time travel does (Q-15 + Q-14).
    for ticker in tickers:
        assert len(await store.events_after(ticker, 0)) > 120  # several snapshots were taken
        for as_of, live_hash in rng.sample(history[ticker], 25):
            raw = await store.latest_snapshot(ticker, as_of)
            book = (
                OrderBook.from_snapshot(BookSnapshot.from_dict(raw)) if raw else OrderBook(ticker)
            )
            for envelope in await store.events_after(ticker, book.sequence, until=as_of):
                book.apply(from_envelope(envelope))
            assert book.state_hash() == live_hash, f"{ticker} at {as_of}"

    restarted = MatchingEngine(store, RecordingPublisher(), environment="dev", new_id=id_source())
    await restarted.recover()
    assert restarted.books_hash() == engine.books_hash()


async def test_the_snapshot_pick_reads_the_right_one_among_many(store: CassandraEventStore) -> None:
    for n in range(1, 31):  # a snapshot every minute for half an hour
        at = DAY1 - timedelta(minutes=31 - n)
        await store.save_snapshot(
            "CALVERA",
            {
                "ticker": "CALVERA",
                "sequence": 100 * n,
                "as_of": stamp(at),
                "bids": [],
                "asks": [],
                "closed": [],
            },
        )
    newest = await store.latest_snapshot("CALVERA")
    assert newest is not None and newest["sequence"] == 3000
    exact = await store.latest_snapshot("CALVERA", stamp(DAY1 - timedelta(minutes=21)))
    assert exact is not None and exact["sequence"] == 1000  # taken exactly at T counts
    between = await store.latest_snapshot("CALVERA", stamp(DAY1 - timedelta(minutes=20, seconds=1)))
    assert between is not None and between["sequence"] == 1000
    assert await store.latest_snapshot("CALVERA", stamp(DAY1 - timedelta(hours=2))) is None


def blank(sequence: int, at: datetime, ticker: str = "CALVERA") -> dict[str, Any]:
    return {"ticker": ticker, "sequence": sequence, "as_of": stamp(at), "bids": [], "asks": []}


def stored(session: Any, ticker: str = "CALVERA") -> dict[int, int]:
    """Every stored snapshot of a ticker: sequence -> hour partition (ms). A test-only full scan."""
    rows = session.execute("SELECT ticker, hour, sequence FROM book_snapshots_by_hour")
    return {
        r.sequence: int(r.hour.replace(tzinfo=UTC).timestamp() * 1000)
        for r in rows
        if r.ticker == ticker
    }


async def test_a_seek_early_in_an_hour_finds_the_snapshot_in_the_hour_before(
    store: CassandraEventStore,
) -> None:
    ten = datetime(2026, 11, 21, 10, 0, tzinfo=UTC)
    await store.save_snapshot("CALVERA", blank(500, ten + timedelta(minutes=50)))
    await store.save_snapshot("CALVERA", blank(1000, ten + timedelta(minutes=80)))  # 11:20
    early = await store.latest_snapshot("CALVERA", stamp(ten + timedelta(minutes=65)))
    assert early is not None and early["sequence"] == 500
    late = await store.latest_snapshot("CALVERA", stamp(ten + timedelta(minutes=85)))
    assert late is not None and late["sequence"] == 1000
    assert await store.latest_snapshot("CALVERA", stamp(ten + timedelta(minutes=40))) is None


async def test_pruning_in_cassandra_keeps_exactly_what_the_rule_keeps(
    session: Any, store: CassandraEventStore
) -> None:
    """27 hours of snapshots two minutes apart, pruned as they are taken: the store ends with the
    rule's set over the whole history, one hour partition each, and the old hours thinned."""
    start = datetime(2026, 11, 21, 0, 7, tzinfo=UTC)
    every: list[tuple[int, int]] = []
    for n in range(1, 27 * 30 + 1):
        at = start + timedelta(minutes=2 * n)
        await store.save_snapshot("CALVERA", blank(n, at))
        every.append((n, ms_of(stamp(at))))
        if n % 15 == 0:
            await store.prune_snapshots("CALVERA", stamp(at))
    now = every[-1][1]
    await store.prune_snapshots("CALVERA", stamp(datetime.fromtimestamp(now / 1000, UTC)))
    expected = snapshots_to_keep(every, now)
    kept = stored(session)
    assert set(kept) == expected
    assert all(kept[s] == hour_of(a) for s, a in every if s in kept)
    hours = {
        int(r.hour.replace(tzinfo=UTC).timestamp() * 1000): r.thinned
        for r in session.execute(
            "SELECT hour, thinned FROM snapshot_hours WHERE ticker = 'CALVERA'"
        )
    }
    assert set(hours) == set(kept.values())  # no listed hour without snapshots, and none missing
    assert all(hours[h] for h in hours if h + HOUR_MS <= now - HOUR_MS)
    # A seek anywhere in the last 24 hours still finds a snapshot within 10 minutes before it.
    for minutes_back in range(5, 24 * 60, 37):
        seek = now - minutes_back * 60_000
        moment = stamp(datetime.fromtimestamp(seek / 1000, UTC))
        raw = await store.latest_snapshot("CALVERA", moment)
        assert raw is not None and seek - ms_of(raw["as_of"]) < 10 * 60_000


async def test_the_first_snapshot_table_is_moved_and_dropped_and_the_engine_rebuilds_the_same(
    session: Any, store: CassandraEventStore
) -> None:
    """Upgrade from the table with one partition per ticker: what retention keeps is copied,
    the old table is dropped, and an engine restarted afterwards has the same books."""

    class NeverPrunes(CassandraEventStore):  # as engines before this change: every snapshot kept
        async def prune_snapshots(self, ticker: str, now: str) -> int:
            return 0

    old_store = NeverPrunes(session)
    await old_store.migrate()
    clock = iter(DAY1 - timedelta(hours=3) + timedelta(seconds=20 * n) for n in count())
    engine = MatchingEngine(
        old_store,
        RecordingPublisher(),
        environment="dev",
        snapshot_every=2,
        clock=lambda: next(clock),
        new_id=id_source(),
    )
    await engine.recover()
    for n in range(1, 401):  # ~2 h 15 min of event time, a snapshot every 40 s
        side = "BUY" if n % 2 else "SELL"
        await engine.handle(
            limit(n, (ALICE, BOB)[n % 2], side, 1, 24500 + 5 * (n % 7), placed_at=stamp(DAY1))
        )
    before = engine.books_hash()
    # The old layout: every snapshot in one partition per ticker, as Phase 2-5 engines wrote it.
    session.execute(
        "CREATE TABLE book_snapshots (ticker text, sequence bigint, as_of timestamp, state text, "
        "PRIMARY KEY (ticker, sequence)) WITH CLUSTERING ORDER BY (sequence DESC)"
    )
    old = list(session.execute("SELECT ticker, sequence, as_of, state FROM book_snapshots_by_hour"))
    for r in old:
        session.execute(
            "INSERT INTO book_snapshots (ticker, sequence, as_of, state) VALUES (%s, %s, %s, %s)",
            (r.ticker, r.sequence, r.as_of, r.state),
        )
    session.execute("TRUNCATE book_snapshots_by_hour")
    session.execute("TRUNCATE snapshot_hours")
    assert len(old) > 150

    upgraded = CassandraEventStore(session)
    await upgraded.migrate()
    tables = {
        r.table_name
        for r in session.execute(
            "SELECT table_name FROM system_schema.tables WHERE keyspace_name = 'matching_engine_it'"
        )
    }
    assert "book_snapshots" not in tables
    times = [(r.sequence, int(r.as_of.replace(tzinfo=UTC).timestamp() * 1000)) for r in old]
    assert set(stored(session)) == snapshots_to_keep(times, max(a for _, a in times))
    await upgraded.migrate()  # the next start-up finds nothing to move

    restarted = MatchingEngine(
        upgraded, RecordingPublisher(), environment="dev", new_id=id_source()
    )
    await restarted.recover()
    assert restarted.books_hash() == before
