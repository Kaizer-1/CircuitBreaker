"""Example tests: sequence numbers, event metadata, "decide vs apply" (ES and CQRS doc, §4)."""

import dataclasses

import pytest

from matching_engine.domain.book import OrderBook
from matching_engine.domain.model import (
    CancelReason,
    CorruptEventError,
    MarketEvent,
    OrderCancelled,
    OrderExpired,
    OrderState,
    Side,
    TradeExecuted,
)
from tests.builders import ALICE, BOB, CARA, T0, Market, cancel, expire, id_source, limit, oid


def test_sequences_start_at_1_and_are_consecutive_with_no_gaps() -> None:
    m = Market()
    m.submit_all(
        limit(1, BOB, "SELL", 5, 24550),
        limit(2, CARA, "SELL", 5, 24560),
        limit(3, ALICE, "BUY", 10, 24600),
        cancel(9, ALICE),
        expire(10, ALICE),
    )
    assert [e.sequence for e in m.log] == list(range(1, len(m.log) + 1))
    assert m.book.sequence == len(m.log)


def test_each_ticker_has_its_own_sequence() -> None:
    a, b = Market("CHAIWALA"), Market("JUGAAD")
    a.submit_all(limit(1, ALICE, "BUY", 1, 24600), limit(2, ALICE, "BUY", 1, 24600))
    b.submit_all(limit(3, ALICE, "BUY", 1, 118000, ticker="JUGAAD"))
    assert [e.sequence for e in a.log] == [1, 2]
    assert [(e.sequence, e.ticker) for e in b.log] == [(1, "JUGAAD")]


def test_every_event_carries_the_commands_id_as_causation_and_its_correlation_id() -> None:
    m = Market()
    m.submit_all(limit(1, BOB, "SELL", 5, 24550))
    command = limit(2, ALICE, "BUY", 5, 24600)
    events = m.submit(command).events
    assert len(events) == 2
    for event in events:
        assert event.causation_id == command.command_id
        assert event.correlation_id == command.correlation_id
        assert event.occurred_at == T0
    assert len({e.event_id for e in m.log}) == len(m.log)


def test_decide_does_not_change_the_book() -> None:
    m = Market()
    m.submit_all(limit(1, BOB, "SELL", 5, 24550))
    before = m.book.state_hash()
    decision = m.book.decide(limit(2, ALICE, "BUY", 5, 24600), new_id=id_source(), now=T0)
    assert len(decision.events) == 2
    assert m.book.state_hash() == before
    assert m.book.sequence == 1


def test_applying_order_accepted_adds_a_limit_order_at_the_back_of_its_level() -> None:
    m = Market()
    m.submit_all(limit(1, BOB, "BUY", 5, 24500), limit(2, CARA, "BUY", 5, 24500))
    [level] = m.book.bids()
    assert [(o.order_id, o.accepted_sequence) for o in level.orders] == [(oid(1), 1), (oid(2), 2)]


def test_applying_a_trade_reduces_both_orders_and_removes_any_left_at_zero() -> None:
    m = Market()
    m.submit_all(limit(1, BOB, "SELL", 8, 24550))
    m.submit_all(limit(2, ALICE, "BUY", 3, 24550))
    resting = m.book.resting(oid(1))
    assert resting is not None and resting.remaining_quantity == 5
    assert m.book.status_of(oid(2)) is OrderState.FILLED


def test_applying_order_cancelled_removes_the_order() -> None:
    m = Market()
    m.submit_all(limit(1, BOB, "SELL", 8, 24550), cancel(1, BOB))
    assert m.book.asks() == []
    assert m.book.status_of(oid(1)) is OrderState.CANCELLED


def test_applying_order_expired_only_marks_the_id_as_dead() -> None:
    m = Market()
    m.submit_all(limit(1, BOB, "SELL", 8, 24550))
    before_levels = m.book.asks()
    m.submit_all(expire(2, ALICE))
    assert m.book.asks() == before_levels
    assert m.book.status_of(oid(2)) is OrderState.EXPIRED


def _replay(events: list[MarketEvent], ticker: str = "CHAIWALA") -> OrderBook:
    book = OrderBook(ticker)
    for event in events:
        book.apply(event)
    return book


def test_apply_refuses_a_gap_or_repeat_in_the_sequence() -> None:
    m = Market()
    m.submit_all(limit(1, BOB, "SELL", 8, 24550), limit(2, BOB, "SELL", 8, 24560))
    first, second = m.log
    with pytest.raises(CorruptEventError, match="sequence"):
        _replay([second])
    with pytest.raises(CorruptEventError, match="sequence"):
        _replay([first, first])


def test_apply_refuses_an_event_for_another_ticker() -> None:
    m = Market()
    m.submit_all(limit(1, BOB, "SELL", 8, 24550))
    with pytest.raises(CorruptEventError, match="ticker"):
        _replay(m.log, ticker="JUGAAD")


def test_apply_refuses_a_trade_or_cancel_for_an_order_that_is_not_in_the_book() -> None:
    m = Market()
    m.submit_all(limit(1, BOB, "SELL", 8, 24550), limit(2, ALICE, "BUY", 8, 24550))
    accepted_1, _, trade = m.log
    with pytest.raises(CorruptEventError, match="not in the book"):
        _replay([accepted_1, dataclasses.replace(trade, sequence=2)])
    cancelled = OrderCancelled(
        sequence=2,
        ticker="CHAIWALA",
        event_id="e1",
        occurred_at=T0,
        causation_id=None,
        correlation_id="x",
        order_id=oid(5),
        trader_id=ALICE,
        side=Side.SELL,
        cancelled_quantity=1,
        reason=CancelReason.USER_REQUESTED,
    )
    with pytest.raises(CorruptEventError, match="not in the book"):
        _replay([accepted_1, cancelled])


def test_apply_refuses_remaining_quantities_that_do_not_add_up() -> None:
    m = Market()
    m.submit_all(limit(1, BOB, "SELL", 8, 24550), limit(2, ALICE, "BUY", 8, 24550))
    accepted_1, accepted_2, trade = m.log
    assert isinstance(trade, TradeExecuted)
    wrong = dataclasses.replace(trade, buy_order_remaining_quantity=1)
    with pytest.raises(CorruptEventError, match="remaining"):
        _replay([accepted_1, accepted_2, wrong])
    wrong_cancel_size = OrderCancelled(
        sequence=2,
        ticker="CHAIWALA",
        event_id="e2",
        occurred_at=T0,
        causation_id=None,
        correlation_id="x",
        order_id=oid(1),
        trader_id=BOB,
        side=Side.SELL,
        cancelled_quantity=3,
        reason=CancelReason.USER_REQUESTED,
    )
    with pytest.raises(CorruptEventError, match="cancelled_quantity"):
        _replay([accepted_1, wrong_cancel_size])


def test_apply_refuses_an_accepted_order_id_that_is_already_known() -> None:
    m = Market()
    m.submit_all(limit(1, BOB, "SELL", 8, 24550), expire(2, ALICE))
    accepted_1, expired = m.log
    assert isinstance(expired, OrderExpired)
    again = dataclasses.replace(accepted_1, sequence=3)
    with pytest.raises(CorruptEventError, match="already known"):
        _replay([accepted_1, expired, again])
    dead = dataclasses.replace(expired, sequence=3, order_id=oid(1))
    with pytest.raises(CorruptEventError, match="already known"):
        _replay([accepted_1, expired, dead])


def test_apply_refuses_an_event_type_it_does_not_know() -> None:
    @dataclasses.dataclass(frozen=True)
    class Mystery(MarketEvent):
        pass

    mystery = Mystery(
        sequence=1,
        ticker="CHAIWALA",
        event_id="e",
        occurred_at=T0,
        causation_id=None,
        correlation_id="c",
    )
    with pytest.raises(CorruptEventError, match="unknown event"):
        _replay([mystery])


def test_event_types_are_the_catalog_names() -> None:
    m = Market()
    m.submit_all(
        limit(1, BOB, "SELL", 8, 24550), limit(2, ALICE, "BUY", 9, 24550), expire(3, ALICE)
    )
    assert [e.event_type for e in m.log] == [
        "OrderAccepted",
        "OrderAccepted",
        "TradeExecuted",
        "OrderExpired",
    ]
