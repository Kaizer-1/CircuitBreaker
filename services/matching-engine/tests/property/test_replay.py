"""Property-based tests: replaying stored events always rebuilds the same book (roadmap Phase 2).

Replay only ever *applies* events; it never re-runs matching (ADR-0005). These properties show that
applying is enough and is deterministic, from the start, from any snapshot, and through the JSON
envelope the event store keeps.
"""

import json

from hypothesis import given
from hypothesis import strategies as st
from matching_engine.domain.book import OrderBook
from matching_engine.domain.model import BookSnapshot, MarketEvent
from matching_engine.events.market import from_envelope, to_envelope

from matching_engine.runtime.events import EventEnvelope
from tests.property.harness import run
from tests.property.workload import TICKERS, Action, actions


def fold(ticker: str, events: list[MarketEvent], start: OrderBook | None = None) -> OrderBook:
    book = start or OrderBook(ticker)
    for event in events:
        book.apply(event)
    return book


@given(actions)
def test_folding_the_events_from_the_start_rebuilds_the_live_book(workload: list[Action]) -> None:
    trace = run(workload)
    for ticker in TICKERS:
        events = [e for e in trace.events if e.ticker == ticker]
        assert fold(ticker, events).state_hash() == trace.books[ticker].state_hash()


@given(actions, st.integers(min_value=0))
def test_a_snapshot_at_any_point_plus_the_rest_rebuilds_the_live_book(
    workload: list[Action], cut: int
) -> None:
    trace = run(workload)
    for ticker in TICKERS:
        events = [e for e in trace.events if e.ticker == ticker]
        k = cut % (len(events) + 1)
        stored = json.loads(json.dumps(fold(ticker, events[:k]).snapshot().to_dict()))
        restored = OrderBook.from_snapshot(BookSnapshot.from_dict(stored))
        assert fold(ticker, events[k:], restored).state_hash() == trace.books[ticker].state_hash()


@given(actions)
def test_replay_through_the_stored_json_envelope_is_lossless(workload: list[Action]) -> None:
    trace = run(workload)
    for ticker in TICKERS:
        events = [e for e in trace.events if e.ticker == ticker]
        stored = [EventEnvelope.from_json(to_envelope(e, "dev").to_json()) for e in events]
        replayed = [from_envelope(env) for env in stored]
        assert replayed == events
        assert fold(ticker, replayed).state_hash() == trace.books[ticker].state_hash()


@given(actions)
def test_replaying_twice_gives_the_same_hash(workload: list[Action]) -> None:
    events = run(workload).events
    for ticker in TICKERS:
        mine = [e for e in events if e.ticker == ticker]
        assert fold(ticker, mine).state_hash() == fold(ticker, mine).state_hash()


@given(actions)
def test_deciding_the_same_commands_with_the_same_ids_and_clock_gives_the_same_events(
    workload: list[Action],
) -> None:
    assert run(workload).events == run(workload).events
