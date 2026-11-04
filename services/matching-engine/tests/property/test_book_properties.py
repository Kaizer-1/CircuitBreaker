"""Property-based tests (Hypothesis): rules that must hold for EVERY random sequence of orders.

Hypothesis generates thousands of workloads (see tests/property/workload.py), shrinks any failure to
the smallest one, and prints it. Example counts come from the profile in tests/conftest.py.
"""

from hypothesis import given

from matching_engine.domain.model import (
    CancelOrder,
    ExpireOrder,
    OrderAccepted,
    OrderCancelled,
    OrderCancelRejected,
    OrderExpired,
    OrderType,
    PlaceOrder,
    Side,
    TradeExecuted,
)
from tests.builders import id_source
from tests.property.harness import NOW, run
from tests.property.workload import TICKERS, Action, Ledger, actions


@given(actions)
def test_the_book_is_never_crossed_after_a_command(workload: list[Action]) -> None:
    for step in run(workload).steps:
        if step.best_bid is not None and step.best_ask is not None:
            assert step.best_bid < step.best_ask


@given(actions)
def test_a_fill_never_exceeds_either_orders_remaining_quantity(workload: list[Action]) -> None:
    for step in run(workload).steps:
        remaining = dict(step.remaining_before)
        for event in step.decision.events:
            if isinstance(event, OrderAccepted):
                remaining[event.order_id] = event.quantity
            elif isinstance(event, TradeExecuted):
                assert event.quantity >= 1
                for order_id, after in (
                    (event.buy_order_id, event.buy_order_remaining_quantity),
                    (event.sell_order_id, event.sell_order_remaining_quantity),
                ):
                    assert event.quantity <= remaining[order_id]
                    assert after == remaining[order_id] - event.quantity
                    remaining[order_id] = after


@given(actions)
def test_the_trade_price_respects_both_orders_limits(workload: list[Action]) -> None:
    trace = run(workload)
    limits: dict[str, tuple[Side, int]] = {}
    for step in trace.steps:
        if isinstance(step.command, PlaceOrder):
            c = step.command
            limits[c.order_id] = (c.side, c.limit_price_paise or c.protection_price_paise or 0)
        for event in step.decision.events:
            if isinstance(event, TradeExecuted):
                assert event.price_paise <= limits[event.buy_order_id][1]
                assert event.price_paise >= limits[event.sell_order_id][1]
                assert event.price_paise % 5 == 0


@given(actions)
def test_no_trade_ever_has_the_same_buyer_and_seller(workload: list[Action]) -> None:
    for event in run(workload).events:
        if isinstance(event, TradeExecuted):
            assert event.buyer_trader_id != event.seller_trader_id


@given(actions)
def test_trading_conserves_total_cash_and_total_shares_per_ticker(workload: list[Action]) -> None:
    trace = run(workload)
    fresh = Ledger()
    assert trace.ledger.total_cash() == fresh.total_cash()
    for ticker in TICKERS:
        assert trace.ledger.total_shares(ticker) == fresh.total_shares(ticker)


@given(actions)
def test_no_balance_or_holding_ever_goes_negative_or_below_what_is_reserved(
    workload: list[Action],
) -> None:
    trace = run(workload)
    assert trace.ledger.problems == []  # every trade was covered by its reservations
    for problems in trace.ledger_problems_per_step:
        assert problems == []


@given(actions)
def test_every_order_quantity_is_filled_resting_or_cancelled_never_lost(
    workload: list[Action],
) -> None:
    trace = run(workload)
    quantity: dict[str, int] = {}
    filled: dict[str, int] = {}
    cancelled: dict[str, int] = {}
    for event in trace.events:
        if isinstance(event, OrderAccepted):
            quantity[event.order_id] = event.quantity
        elif isinstance(event, TradeExecuted):
            for order_id in (event.buy_order_id, event.sell_order_id):
                filled[order_id] = filled.get(order_id, 0) + event.quantity
        elif isinstance(event, OrderCancelled):
            cancelled[event.order_id] = event.cancelled_quantity
    resting = {
        o.order_id: o.remaining_quantity
        for book in trace.books.values()
        for lv in book.bids() + book.asks()
        for o in lv.orders
    }
    for order_id, total in quantity.items():
        assert (
            filled.get(order_id, 0) + resting.get(order_id, 0) + cancelled.get(order_id, 0) == total
        )


@given(actions)
def test_market_orders_never_rest_in_the_book(workload: list[Action]) -> None:
    trace = run(workload)
    market_ids = {
        s.command.order_id
        for s in trace.steps
        if isinstance(s.command, PlaceOrder) and s.command.order_type is OrderType.MARKET
    }
    for book in trace.books.values():
        assert market_ids.isdisjoint(
            o.order_id for lv in book.bids() + book.asks() for o in lv.orders
        )


@given(actions)
def test_sequences_are_gapless_and_strictly_increasing_per_ticker(workload: list[Action]) -> None:
    trace = run(workload)
    for ticker in TICKERS:
        sequences = [e.sequence for e in trace.events if e.ticker == ticker]
        assert sequences == list(range(1, len(sequences) + 1))
        assert trace.books[ticker].sequence == len(sequences)


@given(actions)
def test_each_order_is_accepted_once_and_each_cancel_or_expiry_gets_one_answer(
    workload: list[Action],
) -> None:
    trace = run(workload)
    accepted = [e.order_id for e in trace.events if isinstance(e, OrderAccepted)]
    assert len(accepted) == len(set(accepted))
    for step in trace.steps:
        answers = [
            e
            for e in step.decision.events
            if isinstance(e, OrderCancelled | OrderCancelRejected | OrderExpired)
            and e.order_id == step.command.order_id
        ]
        if isinstance(step.command, CancelOrder):
            assert len(answers) == 1
        elif isinstance(step.command, ExpireOrder):
            assert len(answers) <= 1  # none when the order had already finished


@given(actions)
def test_an_expired_order_id_never_trades(workload: list[Action]) -> None:
    trace = run(workload)
    expired = {e.order_id for e in trace.events if isinstance(e, OrderExpired)}
    for event in trace.events:
        if isinstance(event, TradeExecuted):
            assert event.buy_order_id not in expired and event.sell_order_id not in expired
        if isinstance(event, OrderAccepted):
            assert event.order_id not in expired


@given(actions)
def test_cancelling_everything_at_the_end_leaves_no_money_reserved(workload: list[Action]) -> None:
    trace = run(workload)
    new_id = id_source("f0f0f0f0")
    for book in trace.books.values():
        for lv in book.bids() + book.asks():
            for o in lv.orders:
                decision = book.decide(
                    CancelOrder("cmd", "end", book.ticker, o.order_id, o.trader_id, NOW),
                    new_id=new_id,
                    now=NOW,
                )
                for event in decision.events:
                    book.apply(event)
                    trace.ledger.apply(event)
    assert all(v == 0 for v in trace.ledger.reserved_cash.values())
    assert all(v == 0 for v in trace.ledger.reserved_shares.values())
