"""Example tests: snapshots (policy P-03, every 500 events) and the canonical book hash."""

import json

import pytest

from matching_engine.domain.book import OrderBook
from matching_engine.domain.model import BookSnapshot, CorruptEventError, OrderState
from tests.builders import (
    ALICE,
    BOB,
    CARA,
    T0,
    Market,
    cancel,
    expire,
    id_source,
    limit,
    market,
    oid,
)


def busy_market(id_prefix: str = "e0e0e0e0") -> Market:
    m = Market(id_prefix=id_prefix)
    m.submit_all(
        limit(1, BOB, "SELL", 20, 24550),
        limit(2, CARA, "SELL", 10, 24560),
        limit(3, ALICE, "BUY", 25, 24600),  # trades 20 + 5, order 2 keeps 5
        limit(4, BOB, "BUY", 7, 24400),
        limit(5, CARA, "BUY", 3, 24400),
        cancel(5, CARA),
        expire(6, ALICE),
        market(7, ALICE, "BUY", 2, 27005),
    )
    return m


def test_a_snapshot_survives_a_round_trip_through_json() -> None:
    m = busy_market()
    stored = json.dumps(m.book.snapshot().to_dict())
    restored = OrderBook.from_snapshot(BookSnapshot.from_dict(json.loads(stored)))
    assert restored.state_hash() == m.book.state_hash()
    assert restored.sequence == m.book.sequence
    assert restored.as_of == m.book.as_of == T0
    assert restored.bids() == m.book.bids()
    assert restored.asks() == m.book.asks()
    for n in range(1, 9):
        assert restored.status_of(oid(n)) == m.book.status_of(oid(n))
    assert restored.status_of(oid(6)) is OrderState.EXPIRED


def test_a_restored_book_decides_exactly_like_the_original() -> None:
    m = busy_market()
    restored = OrderBook.from_snapshot(BookSnapshot.from_dict(m.book.snapshot().to_dict()))
    command = limit(9, BOB, "SELL", 12, 24400)
    original = m.book.decide(command, new_id=id_source(), now=T0)
    again = restored.decide(command, new_id=id_source(), now=T0)
    assert original == again


def test_snapshot_plus_the_events_after_it_equals_folding_every_event() -> None:
    m = busy_market()
    half = len(m.log) // 2
    folded_half = OrderBook("CALVERA")
    for event in m.log[:half]:
        folded_half.apply(event)
    from_snapshot = OrderBook.from_snapshot(folded_half.snapshot())
    for event in m.log[half:]:
        from_snapshot.apply(event)
    assert from_snapshot.state_hash() == m.book.state_hash()


def test_an_empty_book_snapshots_at_sequence_0() -> None:
    snapshot = OrderBook("KORVIN").snapshot()
    assert (snapshot.ticker, snapshot.sequence, snapshot.as_of) == ("KORVIN", 0, None)
    assert OrderBook.from_snapshot(snapshot).state_hash() == OrderBook("KORVIN").state_hash()


def test_the_hash_ignores_event_ids_but_not_the_state() -> None:
    a, b = busy_market(), busy_market(id_prefix="f1f1f1f1")  # other event/trade IDs, same market
    assert {e.event_id for e in a.log}.isdisjoint({e.event_id for e in b.log})
    assert a.book.state_hash() == b.book.state_hash()
    b.submit_all(limit(10, BOB, "SELL", 1, 24900))
    assert a.book.state_hash() != b.book.state_hash()


def test_the_hash_changes_when_any_remaining_quantity_changes() -> None:
    a, b = Market(), Market()
    a.submit_all(limit(1, BOB, "SELL", 5, 24550))
    b.submit_all(limit(1, BOB, "SELL", 6, 24550))
    assert a.book.state_hash() != b.book.state_hash()


def test_a_snapshot_cannot_be_taken_halfway_through_a_market_order() -> None:
    m = Market()
    decision = m.book.decide(market(1, ALICE, "BUY", 5, 27005), new_id=id_source(), now=T0)
    m.book.apply(decision.events[0])  # OrderAccepted only: the market order is still being matched
    with pytest.raises(CorruptEventError, match="market order"):
        m.book.snapshot()


def test_a_snapshot_with_a_crossed_book_is_refused_on_restore() -> None:
    snapshot = busy_market().book.snapshot().to_dict()
    snapshot["bids"] = [[24700, [[oid(20), ALICE, 1, 30]]]]
    snapshot["asks"] = [[24600, [[oid(21), BOB, 1, 31]]]]
    with pytest.raises(CorruptEventError, match="crossed"):
        OrderBook.from_snapshot(BookSnapshot.from_dict(snapshot))
