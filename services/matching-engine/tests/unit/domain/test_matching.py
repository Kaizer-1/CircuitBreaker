"""Example tests: one per matching rule (docs/03-architecture/03-event-catalog.md, matching.v1)."""

from matching_engine.domain.model import (
    CancelReason,
    OrderAccepted,
    OrderCancelled,
    OrderState,
    OrderType,
    Side,
    TradeExecuted,
)

from tests.builders import ALICE, BOB, CARA, DEV, Market, limit, market, oid


def trades(events: list[object]) -> list[TradeExecuted]:
    return [e for e in events if isinstance(e, TradeExecuted)]


def resting(m: Market, side: Side) -> list[tuple[int, list[tuple[str, int]]]]:
    levels = m.book.bids() if side is Side.BUY else m.book.asks()
    return [
        (lv.price_paise, [(o.order_id, o.remaining_quantity) for o in lv.orders]) for lv in levels
    ]


# ------------------------------------------------- LIMIT orders


def test_limit_buy_on_an_empty_book_rests_as_a_bid_and_only_is_accepted() -> None:
    m = Market()
    events = m.submit_all(limit(1, ALICE, "BUY", 50, 24600))
    assert [type(e) for e in events] == [OrderAccepted]
    accepted = events[0]
    assert isinstance(accepted, OrderAccepted)
    assert (accepted.order_type, accepted.limit_price_paise, accepted.protection_price_paise) == (
        OrderType.LIMIT,
        24600,
        None,
    )
    assert resting(m, Side.BUY) == [(24600, [(oid(1), 50)])]
    assert m.book.best_bid() == 24600
    assert m.book.best_ask() is None


def test_limit_sell_on_an_empty_book_rests_as_an_ask() -> None:
    m = Market()
    m.submit_all(limit(1, ALICE, "SELL", 20, 24550))
    assert resting(m, Side.SELL) == [(24550, [(oid(1), 20)])]
    assert m.book.status_of(oid(1)) is OrderState.RESTING


def test_limit_orders_that_do_not_cross_both_rest_without_trading() -> None:
    m = Market()
    events = m.submit_all(limit(1, ALICE, "BUY", 10, 24500), limit(2, BOB, "SELL", 10, 24550))
    assert trades(events) == []
    assert (m.book.best_bid(), m.book.best_ask()) == (24500, 24550)


def test_crossing_limit_buy_trades_at_the_resting_asks_price() -> None:
    # Sequence diagram 3: willing to pay 246.00, pays 245.50 (price improvement).
    m = Market()
    m.submit_all(limit(1, BOB, "SELL", 20, 24550))
    [trade] = trades(m.submit_all(limit(2, ALICE, "BUY", 20, 24600)))
    assert trade.price_paise == 24550
    assert trade.quantity == 20
    assert (trade.buy_order_id, trade.sell_order_id) == (oid(2), oid(1))
    assert (trade.buyer_trader_id, trade.seller_trader_id) == (ALICE, BOB)
    assert trade.aggressor_side is Side.BUY


def test_crossing_limit_sell_trades_at_the_resting_bids_price() -> None:
    m = Market()
    m.submit_all(limit(1, ALICE, "BUY", 10, 24600))
    [trade] = trades(m.submit_all(limit(2, BOB, "SELL", 10, 24500)))
    assert trade.price_paise == 24600
    assert trade.aggressor_side is Side.SELL


def test_equal_prices_cross() -> None:
    m = Market()
    m.submit_all(limit(1, BOB, "SELL", 5, 24550))
    [trade] = trades(m.submit_all(limit(2, ALICE, "BUY", 5, 24550)))
    assert trade.price_paise == 24550
    assert m.book.best_bid() is None and m.book.best_ask() is None


# ------------------------------------------------- price-time priority


def test_the_best_price_matches_first() -> None:
    m = Market()
    m.submit_all(limit(1, BOB, "SELL", 10, 24600), limit(2, CARA, "SELL", 10, 24550))
    [trade] = trades(m.submit_all(limit(3, ALICE, "BUY", 10, 24600)))
    assert (trade.sell_order_id, trade.price_paise) == (oid(2), 24550)


def test_at_the_same_price_the_earliest_order_matches_first() -> None:
    m = Market()
    m.submit_all(limit(1, BOB, "SELL", 10, 24550), limit(2, CARA, "SELL", 10, 24550))
    [trade] = trades(m.submit_all(limit(3, ALICE, "BUY", 10, 24550)))
    assert trade.sell_order_id == oid(1)
    assert resting(m, Side.SELL) == [(24550, [(oid(2), 10)])]


def test_an_incoming_order_sweeps_levels_in_price_then_time_order() -> None:
    m = Market()
    m.submit_all(
        limit(1, BOB, "SELL", 5, 24600),
        limit(2, CARA, "SELL", 5, 24550),
        limit(3, DEV, "SELL", 5, 24550),
        limit(4, BOB, "SELL", 5, 24700),
    )
    sweep = trades(m.submit_all(limit(5, ALICE, "BUY", 15, 24600)))
    assert [(t.sell_order_id, t.price_paise) for t in sweep] == [
        (oid(2), 24550),
        (oid(3), 24550),
        (oid(1), 24600),
    ]
    assert resting(m, Side.SELL) == [(24700, [(oid(4), 5)])]
    assert m.book.best_bid() is None


def test_a_later_order_at_a_better_price_jumps_the_queue() -> None:
    m = Market()
    m.submit_all(limit(1, BOB, "BUY", 10, 24500), limit(2, CARA, "BUY", 10, 24510))
    [trade] = trades(m.submit_all(limit(3, ALICE, "SELL", 10, 24500)))
    assert trade.buy_order_id == oid(2)
    assert trade.price_paise == 24510


def test_bids_are_listed_high_to_low_and_asks_low_to_high() -> None:
    m = Market()
    m.submit_all(
        limit(1, BOB, "BUY", 1, 24400),
        limit(2, BOB, "BUY", 1, 24500),
        limit(3, CARA, "SELL", 1, 24700),
        limit(4, CARA, "SELL", 1, 24600),
    )
    assert [lv.price_paise for lv in m.book.bids()] == [24500, 24400]
    assert [lv.price_paise for lv in m.book.asks()] == [24600, 24700]


# ------------------------------------------------- partial fills


def test_a_larger_incoming_order_fills_partly_and_rests_the_remainder() -> None:
    # Sequence diagram 3: BUY 50 @ 246.00 against a resting SELL 20 @ 245.50.
    m = Market()
    m.submit_all(limit(1, BOB, "SELL", 20, 24550))
    [trade] = trades(m.submit_all(limit(2, ALICE, "BUY", 50, 24600)))
    assert trade.quantity == 20
    assert (trade.buy_order_remaining_quantity, trade.sell_order_remaining_quantity) == (30, 0)
    assert resting(m, Side.BUY) == [(24600, [(oid(2), 30)])]
    assert m.book.status_of(oid(1)) is OrderState.FILLED


def test_a_smaller_incoming_order_partly_fills_a_resting_order_that_keeps_its_priority() -> None:
    m = Market()
    m.submit_all(limit(1, BOB, "SELL", 30, 24550), limit(2, CARA, "SELL", 30, 24550))
    m.submit_all(limit(3, ALICE, "BUY", 10, 24550))
    assert resting(m, Side.SELL) == [(24550, [(oid(1), 20), (oid(2), 30)])]
    [trade] = trades(m.submit_all(limit(4, DEV, "BUY", 5, 24550)))
    assert trade.sell_order_id == oid(1)


def test_every_trade_reports_both_remaining_quantities() -> None:
    m = Market()
    m.submit_all(limit(1, BOB, "SELL", 7, 24550), limit(2, CARA, "SELL", 7, 24560))
    sweep = trades(m.submit_all(limit(3, ALICE, "BUY", 10, 24600)))
    assert [(t.buy_order_remaining_quantity, t.sell_order_remaining_quantity) for t in sweep] == [
        (3, 0),
        (0, 4),
    ]


def test_a_filled_resting_order_and_its_empty_level_leave_the_book() -> None:
    m = Market()
    m.submit_all(limit(1, BOB, "SELL", 10, 24550), limit(2, CARA, "SELL", 10, 24600))
    m.submit_all(limit(3, ALICE, "BUY", 10, 24550))
    assert [lv.price_paise for lv in m.book.asks()] == [24600]
    assert m.book.resting(oid(1)) is None
    assert m.book.status_of(oid(3)) is OrderState.FILLED


# ------------------------------------------------- MARKET orders


def test_a_market_buy_sweeps_asks_up_to_its_protection_price() -> None:
    m = Market()
    m.submit_all(limit(1, BOB, "SELL", 5, 24550), limit(2, CARA, "SELL", 5, 27000))
    events = m.submit_all(market(3, ALICE, "BUY", 5, 27005))
    [trade] = trades(events)
    assert trade.price_paise == 24550
    assert not any(isinstance(e, OrderCancelled) for e in events)


def test_a_market_buy_stops_at_its_protection_price_and_the_rest_is_cancelled() -> None:
    m = Market()
    m.submit_all(limit(1, BOB, "SELL", 5, 24550), limit(2, CARA, "SELL", 5, 27010))
    events = m.submit_all(market(3, ALICE, "BUY", 8, 27005))
    assert [t.quantity for t in trades(events)] == [5]
    cancelled = events[-1]
    assert isinstance(cancelled, OrderCancelled)
    assert (cancelled.order_id, cancelled.cancelled_quantity) == (oid(3), 3)
    assert cancelled.reason is CancelReason.UNFILLED_MARKET_ORDER
    assert cancelled.side is Side.BUY
    assert resting(m, Side.SELL) == [(27010, [(oid(2), 5)])]


def test_a_market_sell_stops_at_its_protection_price_and_the_rest_is_cancelled() -> None:
    m = Market()
    m.submit_all(limit(1, BOB, "BUY", 4, 24550), limit(2, CARA, "BUY", 4, 22000))
    events = m.submit_all(market(3, ALICE, "SELL", 6, 22095))
    assert [(t.price_paise, t.quantity) for t in trades(events)] == [(24550, 4)]
    cancelled = events[-1]
    assert isinstance(cancelled, OrderCancelled)
    assert (cancelled.cancelled_quantity, cancelled.reason) == (
        2,
        CancelReason.UNFILLED_MARKET_ORDER,
    )


def test_a_market_order_on_an_empty_book_is_accepted_then_cancelled_in_full() -> None:
    m = Market()
    events = m.submit_all(market(1, ALICE, "BUY", 10, 27005))
    assert [type(e) for e in events] == [OrderAccepted, OrderCancelled]
    accepted, cancelled = events
    assert isinstance(accepted, OrderAccepted) and isinstance(cancelled, OrderCancelled)
    assert (accepted.limit_price_paise, accepted.protection_price_paise) == (None, 27005)
    assert cancelled.cancelled_quantity == 10
    assert m.book.status_of(oid(1)) is OrderState.CANCELLED


def test_a_fully_filled_market_order_emits_no_cancel_and_never_rests() -> None:
    m = Market()
    m.submit_all(limit(1, BOB, "SELL", 10, 24550))
    events = m.submit_all(market(2, ALICE, "BUY", 10, 27005))
    assert [type(e) for e in events] == [OrderAccepted, TradeExecuted]
    assert m.book.resting(oid(2)) is None
    assert m.book.bids() == [] and m.book.asks() == []
    assert m.book.status_of(oid(2)) is OrderState.FILLED


def test_a_market_order_trades_at_resting_prices_not_at_its_protection_price() -> None:
    m = Market()
    m.submit_all(limit(1, BOB, "BUY", 3, 24000), limit(2, CARA, "BUY", 3, 23990))
    sweep = trades(m.submit_all(market(3, ALICE, "SELL", 6, 21600)))
    assert [t.price_paise for t in sweep] == [24000, 23990]


# ------------------------------------------------- self-trade prevention (cancel newest)


def test_an_order_whose_next_match_is_its_own_trader_is_cancelled_instead() -> None:
    m = Market()
    m.submit_all(limit(1, ALICE, "SELL", 10, 24550))
    events = m.submit_all(limit(2, ALICE, "BUY", 10, 24600))
    assert trades(events) == []
    cancelled = events[-1]
    assert isinstance(cancelled, OrderCancelled)
    assert (cancelled.order_id, cancelled.cancelled_quantity) == (oid(2), 10)
    assert cancelled.reason is CancelReason.SELF_TRADE_PREVENTED
    assert resting(m, Side.SELL) == [(24550, [(oid(1), 10)])]  # the resting order is untouched
    assert m.book.bids() == []


def test_trades_before_the_own_order_stand_and_only_the_remainder_is_cancelled() -> None:
    m = Market()
    m.submit_all(limit(1, BOB, "SELL", 4, 24550), limit(2, ALICE, "SELL", 6, 24550))
    events = m.submit_all(limit(3, ALICE, "BUY", 10, 24600))
    assert [(t.sell_order_id, t.quantity) for t in trades(events)] == [(oid(1), 4)]
    cancelled = events[-1]
    assert isinstance(cancelled, OrderCancelled)
    assert (cancelled.cancelled_quantity, cancelled.reason) == (
        6,
        CancelReason.SELF_TRADE_PREVENTED,
    )


def test_self_trade_prevention_applies_to_market_orders_too() -> None:
    m = Market()
    m.submit_all(limit(1, ALICE, "BUY", 5, 24550))
    events = m.submit_all(market(2, ALICE, "SELL", 5, 22095))
    assert trades(events) == []
    cancelled = events[-1]
    assert isinstance(cancelled, OrderCancelled)
    assert cancelled.reason is CancelReason.SELF_TRADE_PREVENTED


def test_an_own_order_that_would_not_match_on_price_does_not_trigger_prevention() -> None:
    m = Market()
    m.submit_all(limit(1, ALICE, "SELL", 5, 24700))
    events = m.submit_all(limit(2, ALICE, "BUY", 5, 24600))
    assert [type(e) for e in events] == [OrderAccepted]
    assert (m.book.best_bid(), m.book.best_ask()) == (24600, 24700)
