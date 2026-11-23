"""Property-based crash tests: kill the engine at random points, restart it, compare with no crash.

A crash can happen before or after any write: the event-store append, a publish, the publish
cursor, a snapshot, or the consumer's offset commit; also while the restarted engine is recovering.
After every crash the engine object is thrown away and a new one is built from the store alone,
and the consumer redelivers every command it had not committed (at-least-once delivery).
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from matching_engine.application.engine import MatchingEngine
from matching_engine.domain.model import TICKERS as ALL_TICKERS
from matching_engine.domain.model import Command
from matching_engine.events.market import to_envelope
from matching_engine.runtime.events import EventEnvelope
from tests.builders import id_source
from tests.fakes import Crash, InMemoryEventStore, RecordingPublisher
from tests.property.harness import Trace, run
from tests.property.workload import TICKERS, Action, actions

NOW = datetime(2026, 11, 20, 10, 42, 1, 250_000, tzinfo=UTC)  # the harness's NOW


async def run_with_crashes(
    commands: list[Command], crash_at_calls: set[int], snapshot_every: int
) -> tuple[MatchingEngine, InMemoryEventStore, RecordingPublisher, int]:
    store, publisher = InMemoryEventStore(), RecordingPublisher()
    calls = 0
    pending = set(crash_at_calls)

    def hook(point: str) -> None:
        nonlocal calls
        calls += 1
        if calls in pending:
            pending.discard(calls)
            raise Crash(point)

    store.hook = hook
    publisher.hook = hook
    new_id = id_source("d0d0d0d0")  # shared by every engine instance: IDs are never reused

    async def boot() -> MatchingEngine:
        while True:
            engine = MatchingEngine(
                store,
                publisher,
                environment="dev",
                snapshot_every=snapshot_every,
                snapshot_spacing_ms=0,
                clock=lambda: NOW,
                new_id=new_id,
            )
            try:
                await engine.recover()
                return engine
            except Crash:
                continue

    engine = await boot()
    committed = crashes = 0
    while committed < len(commands):
        try:
            await engine.handle(commands[committed])
            hook("commit.before")  # the consumer commits the offset only after handle returns
            committed += 1
        except Crash:
            crashes += 1
            engine = await boot()
    return engine, store, publisher, crashes


def shape(data: dict[str, Any]) -> dict[str, Any]:
    """An event's data without the IDs that are freshly generated on every decision."""
    return {k: v for k, v in data.items() if k != "trade_id"}


def clean_envelopes(clean: Trace, ticker: str) -> list[EventEnvelope]:
    return [to_envelope(e, "dev") for e in clean.events if e.ticker == ticker]


def assert_same_market(clean: Trace, engine: MatchingEngine) -> None:
    for ticker in ALL_TICKERS:
        expected = clean.books[ticker].state_hash() if ticker in TICKERS else None
        if expected is not None:
            assert engine.book(ticker).state_hash() == expected
        else:
            assert engine.book(ticker).sequence == 0


@given(
    actions,
    st.sets(st.integers(min_value=1, max_value=600), max_size=8),
    st.integers(min_value=1, max_value=7),
)
def test_a_crash_at_any_point_then_a_rebuild_gives_the_same_books_as_no_crash(
    workload: list[Action], crash_at_calls: set[int], snapshot_every: int
) -> None:
    clean = run(workload)
    commands = [s.command for s in clean.steps]
    engine, store, publisher, _ = asyncio.run(
        run_with_crashes(commands, crash_at_calls, snapshot_every)
    )
    assert_same_market(clean, engine)

    for ticker in TICKERS:
        expected = [(e.event_type, e.sequence) for e in clean.events if e.ticker == ticker]
        stored = store.events[ticker]
        assert [
            (e.event_type, e.data["sequence"]) for e in stored
        ] == expected  # none lost, none twice
        published = publisher.first_copies(ticker)
        assert [e.event_id for e in published] == [e.event_id for e in stored]  # all sent, in order
        assert store.cursors[ticker] == len(stored)
        clean_data = [shape(e.data) for e in clean_envelopes(clean, ticker)]
        assert [shape(e.data) for e in stored] == clean_data  # the same facts, field by field

    # Kill it once more, idle: the rebuilt books equal the books just before the kill.
    before = engine.books_hash()
    restarted = MatchingEngine(
        store,
        publisher,
        environment="dev",
        snapshot_every=snapshot_every,
        snapshot_spacing_ms=0,
        clock=lambda: NOW,
    )
    store.hook = publisher.hook = lambda point: None
    asyncio.run(restarted.recover())
    assert restarted.books_hash() == before


@given(actions, st.lists(st.integers(min_value=0), max_size=20))
def test_redelivered_commands_never_change_the_books(
    workload: list[Action], duplicates: list[int]
) -> None:
    clean = run(workload)
    commands = [s.command for s in clean.steps]
    delivered = list(commands)
    for d in duplicates:  # deliver some commands again, some time after their first copy
        if commands:
            original = commands[d % len(commands)]
            first_copy = delivered.index(original)  # redelivery never overtakes the original
            delivered.insert(min(len(delivered), first_copy + 1 + d % 5), original)

    async def go() -> MatchingEngine:
        engine = MatchingEngine(
            InMemoryEventStore(),
            RecordingPublisher(),
            environment="dev",
            clock=lambda: NOW,
            new_id=id_source(),
        )
        await engine.recover()
        for command in delivered:
            await engine.handle(command)
        return engine

    assert_same_market(clean, asyncio.run(go()))
