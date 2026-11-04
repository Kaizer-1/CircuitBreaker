"""Example tests: cancel requests (policy P-02) and expiry requests (policy P-14, ADR-0017)."""

from matching_engine.domain.model import (
    CancelReason,
    CancelRejectReason,
    OrderCancelled,
    OrderCancelRejected,
    OrderExpired,
    OrderState,
    Outcome,
    TradeExecuted,
)
from tests.builders import ALICE, BOB, Market, cancel, expire, limit, market, oid

# ------------------------------------------------- cancel


def test_cancelling_a_resting_order_removes_it_with_reason_user_requested() -> None:
    m = Market()
    m.submit_all(limit(1, ALICE, "BUY", 30, 24600))
    [cancelled] = m.submit_all(cancel(1, ALICE))
    assert isinstance(cancelled, OrderCancelled)
    assert (cancelled.cancelled_quantity, cancelled.reason) == (30, CancelReason.USER_REQUESTED)
    assert m.book.bids() == []
    assert m.book.status_of(oid(1)) is OrderState.CANCELLED


def test_cancelling_a_partly_filled_order_cancels_only_what_is_left() -> None:
    # Sequence diagram 4: 50 placed, 20 filled, the remaining 30 cancelled.
    m = Market()
    m.submit_all(limit(1, BOB, "SELL", 20, 24550), limit(2, ALICE, "BUY", 50, 24600))
    [cancelled] = m.submit_all(cancel(2, ALICE))
    assert isinstance(cancelled, OrderCancelled)
    assert cancelled.cancelled_quantity == 30


def test_cancelling_an_order_that_filled_a_moment_ago_is_rejected_already_filled() -> None:
    m = Market()
    m.submit_all(limit(1, ALICE, "BUY", 10, 24600), limit(2, BOB, "SELL", 10, 24600))
    [rejected] = m.submit_all(cancel(1, ALICE))
    assert isinstance(rejected, OrderCancelRejected)
    assert rejected.reason is CancelRejectReason.ALREADY_FILLED


def test_cancelling_twice_rejects_the_second_as_already_cancelled() -> None:
    m = Market()
    m.submit_all(limit(1, ALICE, "BUY", 10, 24600), cancel(1, ALICE))
    [rejected] = m.submit_all(cancel(1, ALICE))
    assert isinstance(rejected, OrderCancelRejected)
    assert rejected.reason is CancelRejectReason.ALREADY_CANCELLED


def test_cancelling_the_unfilled_rest_of_a_market_order_is_already_cancelled() -> None:
    m = Market()
    m.submit_all(market(1, ALICE, "BUY", 10, 27005))
    [rejected] = m.submit_all(cancel(1, ALICE))
    assert isinstance(rejected, OrderCancelRejected)
    assert rejected.reason is CancelRejectReason.ALREADY_CANCELLED


def test_cancelling_an_order_the_book_never_had_is_unknown_order() -> None:
    m = Market()
    [rejected] = m.submit_all(cancel(99, ALICE))
    assert isinstance(rejected, OrderCancelRejected)
    assert (rejected.order_id, rejected.reason) == (oid(99), CancelRejectReason.UNKNOWN_ORDER)


def test_cancelling_someone_elses_resting_order_is_unknown_order_and_changes_nothing() -> None:
    m = Market()
    m.submit_all(limit(1, ALICE, "BUY", 10, 24600))
    [rejected] = m.submit_all(cancel(1, BOB))
    assert isinstance(rejected, OrderCancelRejected)
    assert rejected.reason is CancelRejectReason.UNKNOWN_ORDER
    assert m.book.status_of(oid(1)) is OrderState.RESTING


def test_cancelling_someone_elses_filled_order_is_unknown_order() -> None:
    m = Market()
    m.submit_all(limit(1, ALICE, "BUY", 10, 24600), limit(2, BOB, "SELL", 10, 24600))
    [rejected] = m.submit_all(cancel(1, BOB))
    assert isinstance(rejected, OrderCancelRejected)
    assert rejected.reason is CancelRejectReason.UNKNOWN_ORDER


# ------------------------------------------------- expiry (fail-closed orphan rule)


def test_expiring_an_order_the_engine_never_saw_records_a_tombstone() -> None:
    m = Market()
    decision = m.submit(expire(7, ALICE))
    [expired] = decision.events
    assert isinstance(expired, OrderExpired)
    assert (expired.order_id, expired.trader_id) == (oid(7), ALICE)
    assert decision.outcome is Outcome.PROCESSED
    assert m.book.status_of(oid(7)) is OrderState.EXPIRED


def test_a_late_order_placed_with_an_expired_id_is_refused_with_no_events() -> None:
    m = Market()
    m.submit(expire(7, ALICE))
    m.submit_all(limit(8, BOB, "SELL", 10, 24550))
    decision = m.submit(limit(7, ALICE, "BUY", 10, 24600))
    assert decision.events == ()
    assert decision.outcome is Outcome.REFUSED
    assert "expired" in decision.detail
    assert m.book.bids() == []  # it never entered the book, so it can never trade
    assert m.book.status_of(oid(7)) is OrderState.EXPIRED


def test_expiring_a_resting_order_cancels_it_with_reason_order_expired() -> None:
    m = Market()
    m.submit_all(limit(1, BOB, "SELL", 5, 24550), limit(2, ALICE, "BUY", 25, 24600))
    [cancelled] = m.submit_all(expire(2, ALICE))
    assert isinstance(cancelled, OrderCancelled)
    assert (cancelled.cancelled_quantity, cancelled.reason) == (20, CancelReason.ORDER_EXPIRED)
    assert m.book.status_of(oid(2)) is OrderState.CANCELLED


def test_expiring_an_order_that_already_finished_does_nothing() -> None:
    m = Market()
    m.submit_all(limit(1, ALICE, "BUY", 10, 24600), limit(2, BOB, "SELL", 10, 24600))
    decision = m.submit(expire(1, ALICE))
    assert decision.events == ()
    assert decision.outcome is Outcome.NOTHING_TO_DO


def test_expiring_an_already_expired_order_does_nothing() -> None:
    m = Market()
    m.submit(expire(7, ALICE))
    decision = m.submit(expire(7, ALICE))
    assert decision.events == ()
    assert decision.outcome is Outcome.NOTHING_TO_DO


def test_an_expiry_for_someone_elses_resting_order_is_refused() -> None:
    m = Market()
    m.submit_all(limit(1, ALICE, "BUY", 10, 24600))
    decision = m.submit(expire(1, BOB))
    assert decision.events == ()
    assert decision.outcome is Outcome.REFUSED
    assert m.book.status_of(oid(1)) is OrderState.RESTING


def test_cancelling_an_expired_order_is_unknown_order() -> None:
    m = Market()
    m.submit(expire(7, ALICE))
    [rejected] = m.submit_all(cancel(7, ALICE))
    assert isinstance(rejected, OrderCancelRejected)
    assert rejected.reason is CancelRejectReason.UNKNOWN_ORDER


def test_an_expired_order_never_produces_a_trade() -> None:
    m = Market()
    m.submit(expire(7, ALICE))
    events = m.submit_all(limit(8, BOB, "SELL", 10, 24550), limit(7, ALICE, "BUY", 10, 24600))
    assert not any(isinstance(e, TradeExecuted) for e in events)
