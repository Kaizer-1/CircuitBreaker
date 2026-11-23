"""The command loop (ES and CQRS doc section 4): dedupe → decide → append → apply → publish."""

from datetime import UTC, datetime, timedelta

import pytest

from matching_engine.application.engine import MatchingEngine
from matching_engine.domain.model import InvalidCommandError, Outcome
from matching_engine.runtime.events import build_event, format_timestamp, parse_timestamp
from tests.builders import ALICE, BOB, CARA, cancel, expire, id_source, limit, market
from tests.fakes import Crash, InMemoryEventStore, RecordingPublisher

NOW = datetime(2026, 11, 20, 10, 42, 1, 250_000, tzinfo=UTC)


def make_engine(
    store: InMemoryEventStore, publisher: RecordingPublisher, *, snapshot_every: int = 500
) -> MatchingEngine:
    return MatchingEngine(
        store,
        publisher,
        environment="dev",
        snapshot_every=snapshot_every,
        snapshot_spacing_ms=0,  # one fixed clock: every event at the same moment
        clock=lambda: NOW,
        new_id=id_source(),
    )


@pytest.fixture
def store() -> InMemoryEventStore:
    return InMemoryEventStore()


@pytest.fixture
def publisher() -> RecordingPublisher:
    return RecordingPublisher()


async def started(
    store: InMemoryEventStore, publisher: RecordingPublisher, **kw: int
) -> MatchingEngine:
    engine = make_engine(store, publisher, **kw)
    await engine.recover()
    return engine


async def test_events_are_stored_before_they_are_published(
    store: InMemoryEventStore, publisher: RecordingPublisher
) -> None:
    calls: list[str] = []
    store.hook = lambda point: calls.append(point)
    publisher.hook = lambda point: calls.append(point)
    engine = await started(store, publisher)
    await engine.handle(limit(1, ALICE, "BUY", 10, 24600))
    assert calls[:2] == ["append.before", "append.after"]
    assert calls.index("append.after") < calls.index("publish.before")
    assert calls[-1] == "cursor.after"
    assert store.cursors["CALVERA"] == 1


async def test_published_events_have_the_catalog_envelope(
    store: InMemoryEventStore, publisher: RecordingPublisher
) -> None:
    engine = await started(store, publisher)
    command = limit(1, ALICE, "BUY", 10, 24600)
    await engine.handle(command)
    [(ticker, event)] = publisher.sent
    assert ticker == "CALVERA"
    assert (event.event_type, event.producer, event.environment) == (
        "OrderAccepted",
        "matching-engine",
        "dev",
    )
    assert (event.causation_id, event.correlation_id) == (
        command.command_id,
        command.correlation_id,
    )
    assert event.occurred_at == "2026-11-20T10:42:01.250Z"
    assert event.data["accepted_at"] == event.occurred_at
    assert event.data["sequence"] == 1


async def test_a_duplicate_command_is_skipped(
    store: InMemoryEventStore, publisher: RecordingPublisher
) -> None:
    engine = await started(store, publisher)
    command = limit(1, ALICE, "BUY", 10, 24600)
    assert await engine.handle(command) is not None
    assert await engine.handle(command) is None
    assert len(store.events["CALVERA"]) == 1
    assert len(publisher.sent) == 1


async def test_a_failed_publish_keeps_the_cursor_and_a_restart_sends_the_events(
    store: InMemoryEventStore, publisher: RecordingPublisher
) -> None:
    engine = await started(store, publisher)

    def broker_down(point: str) -> None:
        if point == "publish.before":
            raise ConnectionError("redpanda is down")

    publisher.hook = broker_down
    with pytest.raises(ConnectionError):
        await engine.handle(limit(1, ALICE, "BUY", 10, 24600))
    assert len(store.events["CALVERA"]) == 1  # stored: it happened
    assert store.cursors["CALVERA"] == 0  # but not published yet

    publisher.hook = lambda point: None
    restarted = await started(store, publisher)
    assert [e.data["sequence"] for _, e in publisher.sent] == [1]
    assert store.cursors["CALVERA"] == 1
    assert restarted.book("CALVERA").state_hash() == engine.book("CALVERA").state_hash()


async def test_a_redelivered_command_after_a_failed_publish_publishes_the_backlog(
    store: InMemoryEventStore, publisher: RecordingPublisher
) -> None:
    engine = await started(store, publisher)
    command = limit(1, ALICE, "BUY", 10, 24600)
    publisher.hook = lambda point: (
        (_ for _ in ()).throw(ConnectionError()) if point == "publish.before" else None
    )
    with pytest.raises(ConnectionError):
        await engine.handle(command)
    publisher.hook = lambda point: None
    assert await engine.handle(command) is None  # already processed: skip to publishing
    assert [e.data["sequence"] for _, e in publisher.sent] == [1]


async def test_a_restart_publishes_only_what_is_after_the_cursor(
    store: InMemoryEventStore, publisher: RecordingPublisher
) -> None:
    engine = await started(store, publisher)
    await engine.handle(limit(1, BOB, "SELL", 5, 24550))
    await engine.handle(limit(2, ALICE, "BUY", 5, 24550))
    assert len(publisher.sent) == 3
    await started(store, publisher)
    assert len(publisher.sent) == 3


async def test_a_failed_append_leaves_the_book_unchanged(
    store: InMemoryEventStore, publisher: RecordingPublisher
) -> None:
    engine = await started(store, publisher)
    await engine.handle(limit(1, BOB, "SELL", 5, 24550))
    before = engine.book("CALVERA").state_hash()

    def cassandra_down(point: str) -> None:
        if point == "append.before":
            raise TimeoutError("cassandra timed out")

    store.hook = cassandra_down
    command = limit(2, ALICE, "BUY", 5, 24550)
    with pytest.raises(TimeoutError):
        await engine.handle(command)
    assert engine.book("CALVERA").state_hash() == before
    store.hook = lambda point: None
    decision = await engine.handle(command)  # the consumer retries the same command
    assert decision is not None and len(decision.events) == 2


async def test_a_snapshot_is_taken_every_n_events_per_ticker(
    store: InMemoryEventStore, publisher: RecordingPublisher
) -> None:
    engine = await started(store, publisher, snapshot_every=3)
    for n in range(1, 8):
        await engine.handle(limit(n, ALICE, "BUY", 1, 24000 + 5 * n))
    await engine.handle(limit(20, ALICE, "BUY", 1, 118000, ticker="KORVIN"))
    assert [s["sequence"] for s in store.snapshots["CALVERA"]] == [3, 6]
    assert store.snapshots["KORVIN"] == []


async def test_a_failed_snapshot_loses_nothing_and_is_tried_again(
    store: InMemoryEventStore, publisher: RecordingPublisher
) -> None:
    engine = await started(store, publisher, snapshot_every=2)

    def snapshot_fails(point: str) -> None:
        if point == "snapshot.before":
            raise OSError("disk full")

    store.hook = snapshot_fails
    await engine.handle(limit(1, ALICE, "BUY", 1, 24000))
    with pytest.raises(OSError):
        await engine.handle(limit(2, ALICE, "BUY", 1, 24005))
    assert len(store.events["CALVERA"]) == 2
    store.hook = lambda point: None
    await engine.handle(limit(3, ALICE, "BUY", 1, 24010))
    assert [s["sequence"] for s in store.snapshots["CALVERA"]] == [3]
    assert len(publisher.first_copies("CALVERA")) == 3


async def test_recovery_loads_the_latest_snapshot_and_replays_only_the_events_after_it(
    store: InMemoryEventStore, publisher: RecordingPublisher
) -> None:
    engine = await started(store, publisher, snapshot_every=5)
    for n in range(1, 13):
        await engine.handle(limit(n, ALICE, "BUY", 1, 24000 + 5 * n))
    store.reads = 0
    restarted = await started(store, publisher, snapshot_every=5)
    assert store.reads == 2  # sequence 10 is the latest snapshot; events 11 and 12 are replayed
    assert restarted.book("CALVERA").state_hash() == engine.book("CALVERA").state_hash()


async def test_killing_the_engine_and_rebuilding_from_the_store_gives_the_same_hash(
    store: InMemoryEventStore, publisher: RecordingPublisher
) -> None:
    engine = await started(store, publisher, snapshot_every=4)
    for command in [
        limit(1, BOB, "SELL", 20, 24550),
        limit(2, CARA, "SELL", 10, 24560),
        limit(3, ALICE, "BUY", 25, 24600),
        market(4, ALICE, "BUY", 50, 27005),
        limit(5, BOB, "BUY", 7, 24400),
        cancel(5, BOB),
        expire(6, ALICE),
        limit(7, CARA, "BUY", 2, 24300, ticker="KORVIN"),
    ]:
        await engine.handle(command)
    before = engine.books_hash()
    del engine  # kill -9: only the store survives
    assert (await started(store, publisher, snapshot_every=4)).books_hash() == before


async def test_an_invalid_command_raises_and_stores_nothing(
    store: InMemoryEventStore, publisher: RecordingPublisher
) -> None:
    engine = await started(store, publisher)
    with pytest.raises(InvalidCommandError):
        await engine.handle(limit(1, ALICE, "BUY", 10, 24603))
    with pytest.raises(InvalidCommandError, match="ticker"):
        await engine.handle(limit(2, ALICE, "BUY", 10, 24600, ticker="NOPE"))
    assert store.events == {} or all(not v for v in store.events.values())
    assert store.processed == set()


async def test_a_refused_command_is_recorded_as_processed_with_no_events(
    store: InMemoryEventStore, publisher: RecordingPublisher
) -> None:
    engine = await started(store, publisher)
    await engine.handle(expire(7, ALICE))
    late = limit(7, ALICE, "BUY", 10, 24600)
    decision = await engine.handle(late)
    assert decision is not None and decision.outcome is Outcome.REFUSED
    assert late.command_id in store.processed
    assert [e.event_type for e in store.events["CALVERA"]] == ["OrderExpired"]


async def test_handle_envelope_parses_commands_and_ignores_unknown_event_types(
    store: InMemoryEventStore, publisher: RecordingPublisher
) -> None:
    engine = await started(store, publisher)
    placed = build_event(
        "OrderPlaced",
        {
            "order_id": "7f3c9e10-2b4d-4e6f-8a1b-3c5d7e9f0a12",
            "trader_id": ALICE,
            "ticker": "CALVERA",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 50,
            "limit_price_paise": 24600,
            "protection_price_paise": None,
            "reservation_id": "c1d2e3f4-a5b6-4c7d-8e9f-0a1b2c3d4e5f",
            "placed_at": "2026-11-20T10:42:01.248Z",
        },
        producer="order-service",
        environment="dev",
        correlation_id="req-5f1c2a",
    )
    await engine.handle_envelope(placed)
    [(_, accepted)] = publisher.sent
    assert accepted.causation_id == placed.event_id
    unknown = build_event(
        "SomethingNew", {}, producer="order-service", environment="dev", correlation_id="x"
    )
    await engine.handle_envelope(unknown)
    assert len(publisher.sent) == 1


async def test_the_engine_is_not_ready_until_it_has_recovered(
    store: InMemoryEventStore, publisher: RecordingPublisher
) -> None:
    engine = make_engine(store, publisher)
    assert engine.ready is False
    with pytest.raises(RuntimeError, match="recover"):
        await engine.handle(limit(1, ALICE, "BUY", 1, 24600))
    await engine.recover()
    assert engine.ready is True


async def test_a_crash_between_append_and_apply_is_healed_by_recovery(
    store: InMemoryEventStore, publisher: RecordingPublisher
) -> None:
    engine = await started(store, publisher)
    await engine.handle(limit(1, BOB, "SELL", 5, 24550))

    def die_after_append(point: str) -> None:
        if point == "append.after":
            raise Crash

    store.hook = die_after_append
    command = limit(2, ALICE, "BUY", 5, 24550)
    with pytest.raises(Crash):
        await engine.handle(command)
    store.hook = lambda point: None
    restarted = await started(store, publisher)
    assert await restarted.handle(command) is None  # the redelivered command is a duplicate
    assert restarted.book("CALVERA").asks() == []
    assert [e.event_type for e in publisher.first_copies("CALVERA")] == [
        "OrderAccepted",
        "OrderAccepted",
        "TradeExecuted",
    ]


async def test_an_append_that_timed_out_but_was_written_is_found_by_rebuilding(
    store: InMemoryEventStore, publisher: RecordingPublisher
) -> None:
    # A Cassandra write timeout does not mean the logged batch failed: it may still be applied.
    engine = await started(store, publisher)

    def timeout_after_write(point: str) -> None:
        if point == "append.after":
            raise TimeoutError("write timed out, but the batch was written")

    store.hook = timeout_after_write
    command = limit(1, ALICE, "BUY", 10, 24600)
    with pytest.raises(TimeoutError):
        await engine.handle(command)
    store.hook = lambda point: None
    assert await engine.handle(command) is None  # rebuilt from the store: already processed
    assert engine.book("CALVERA").best_bid() == 24600
    await engine.handle(limit(2, BOB, "SELL", 4, 24600))
    assert [e.data["sequence"] for e in store.events["CALVERA"]] == [1, 2, 3]
    assert [e.data["sequence"] for e in publisher.first_copies("CALVERA")] == [1, 2, 3]


class SteppingClock:
    """Each command `step` seconds after the one before: event time moves as in real trading."""

    def __init__(self, step: float) -> None:
        self.now, self.step = NOW, step

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=self.step)
        return self.now


async def test_snapshots_are_at_least_30_seconds_of_event_time_apart(
    store: InMemoryEventStore, publisher: RecordingPublisher
) -> None:
    # Busy trading: 2 events every 10 s reach snapshot_every=2 at every order, but P-03 allows
    # one snapshot per 30 s per ticker.
    engine = MatchingEngine(
        store,
        publisher,
        environment="dev",
        snapshot_every=2,
        clock=SteppingClock(10),
        new_id=id_source(),
    )
    await engine.recover()
    for n in range(1, 13):
        await engine.handle(limit(n, ALICE, "BUY", 1, 24000 + 5 * n))
    times = [parse_timestamp(s["as_of"]) for s in store.snapshots["CALVERA"]]
    assert len(times) == 4
    assert all(b - a >= timedelta(seconds=30) for a, b in zip(times, times[1:], strict=False))


async def test_old_snapshots_are_pruned_after_each_new_one(
    store: InMemoryEventStore, publisher: RecordingPublisher
) -> None:
    # One order a minute for three hours: snapshots older than an hour are thinned to one per
    # 10-minute slot (domain/retention.py); the events are all still there.
    engine = MatchingEngine(
        store,
        publisher,
        environment="dev",
        snapshot_every=1,
        clock=SteppingClock(60),
        new_id=id_source(),
    )
    await engine.recover()
    for n in range(1, 181):
        placed = format_timestamp(NOW + timedelta(minutes=n))  # fresh: within the 1-hour memory
        await engine.handle(limit(n, ALICE, "BUY", 1, 20000 + 5 * n, placed_at=placed))
    kept = store.snapshots["CALVERA"]
    assert len(store.events["CALVERA"]) == 180
    # Orders at 10:43:01 ... 13:42:01: the last hour's 61 snapshots (both ends), and before that
    # the earliest of each 10-minute slot from 10:40 to 12:40 (13 slots).
    assert len(kept) == 61 + 13
    restarted = await started(store, publisher)
    assert restarted.book("CALVERA").state_hash() == engine.book("CALVERA").state_hash()


async def test_a_failed_pruning_does_not_fail_the_command(
    store: InMemoryEventStore, publisher: RecordingPublisher
) -> None:
    engine = await started(store, publisher, snapshot_every=1)

    async def broken(ticker: str, now: str) -> int:
        raise OSError("Cassandra timeout")

    store.prune_snapshots = broken  # type: ignore[method-assign]
    decision = await engine.handle(limit(1, ALICE, "BUY", 1, 24000))
    assert decision is not None and len(store.snapshots["CALVERA"]) == 1
