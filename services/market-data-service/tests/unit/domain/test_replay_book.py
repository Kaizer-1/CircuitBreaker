"""ReplayBook: the whole book rebuilt by applying stored events (apply only, never decide)."""

from typing import Any

import pytest

from market_data_service.domain.depth import Level
from market_data_service.domain.replay_book import CorruptHistoryError, ReplayBook
from tests.fakes import Exchange

T = "2026-11-20T10:42:01.250Z"


def accepted(sequence: int, order_id: str, side: str, quantity: int, price: int | None) -> Any:
    return {
        "sequence": sequence,
        "order_id": order_id,
        "trader_id": f"trader-{order_id}",
        "side": side,
        "order_type": "LIMIT" if price is not None else "MARKET",
        "quantity": quantity,
        "limit_price_paise": price,
    }


def trade(sequence: int, buy: str, sell: str, quantity: int, left: tuple[int, int]) -> Any:
    return {
        "sequence": sequence,
        "trade_id": f"t{sequence}",
        "quantity": quantity,
        "buy_order_id": buy,
        "sell_order_id": sell,
        "buy_order_remaining_quantity": left[0],
        "sell_order_remaining_quantity": left[1],
    }


def test_events_build_the_book_in_price_time_priority_with_every_finished_order() -> None:
    book = ReplayBook("CALVERA")
    book.apply("OrderAccepted", accepted(1, "s1", "SELL", 20, 24550), T)
    book.apply("OrderAccepted", accepted(2, "s2", "SELL", 10, 24550), T)
    book.apply("OrderAccepted", accepted(3, "b1", "BUY", 5, 24500), T)
    book.apply("OrderAccepted", accepted(4, "m1", "BUY", 25, None), T)  # a MARKET order
    book.apply("TradeExecuted", trade(5, "m1", "s1", 20, (5, 0)), T)
    book.apply("TradeExecuted", trade(6, "m1", "s2", 5, (0, 5)), T)
    book.apply("OrderCancelRejected", {"sequence": 7, "order_id": "zz"}, T)
    book.apply("OrderExpired", {"sequence": 8, "order_id": "x1", "trader_id": "trader-x1"}, T)
    book.apply("OrderCancelled", {"sequence": 9, "order_id": "b1", "cancelled_quantity": 5}, T)
    assert book.depth(10) == ([], [Level(24550, 5, 1)])
    canonical = book.canonical()
    assert (canonical["sequence"], canonical["as_of"]) == (9, T)
    assert canonical["asks"] == [[24550, [["s2", "trader-s2", 5, 2]]]]
    assert canonical["closed"] == [
        ["b1", "CANCELLED", "trader-b1"],
        ["m1", "FILLED", "trader-m1"],
        ["s1", "FILLED", "trader-s1"],
        ["x1", "EXPIRED", "trader-x1"],
    ]


def test_depth_shows_the_best_levels_first_and_aggregates_each_price() -> None:
    book = ReplayBook("CALVERA")
    for n, (side, qty, price) in enumerate(
        [("BUY", 5, 100), ("BUY", 7, 105), ("BUY", 1, 105), ("SELL", 3, 110), ("SELL", 4, 120)],
        start=1,
    ):
        book.apply("OrderAccepted", accepted(n, f"o{n}", side, qty, price), T)
    assert book.depth(1) == ([Level(105, 8, 2)], [Level(110, 3, 1)])
    assert book.depth(5)[0] == [Level(105, 8, 2), Level(100, 5, 1)]


def test_a_rebuild_from_a_snapshot_continues_exactly_like_the_original() -> None:
    exchange = Exchange()
    exchange.place("a", "SELL", 10, 500)
    exchange.place("b", "BUY", 4, 500)
    snap = exchange.snapshot()
    book = ReplayBook.from_snapshot(
        "CALVERA",
        sequence=snap.sequence,
        as_of=snap.as_of,
        bids=snap.bids,
        asks=snap.asks,
        closed=snap.closed,
    )
    assert book.state_hash() == exchange.state_hash()
    for event in exchange.place("c", "BUY", 6, 505) + exchange.expire_unseen("a"):
        book.apply(event.event_type, event.data, event.occurred_at)
    assert book.state_hash() == exchange.state_hash()


def test_sequence_0_is_the_empty_book_before_any_event_whatever_as_of_q15_sent() -> None:
    book = ReplayBook.from_snapshot(
        "TAVIRA", sequence=0, as_of="2026-11-20T11:00:00.000Z", bids=[], asks=[], closed=[]
    )
    assert book.canonical()["as_of"] is None
    assert book.state_hash() == ReplayBook("TAVIRA").state_hash() == Exchange("TAVIRA").state_hash()


@pytest.mark.parametrize(
    ("events", "message"),
    [
        ([("OrderAccepted", accepted(2, "o1", "BUY", 1, 100))], "does not follow"),
        (
            [
                ("OrderAccepted", accepted(1, "o1", "BUY", 1, 100)),
                ("OrderAccepted", accepted(2, "o1", "BUY", 1, 100)),
            ],
            "already known",
        ),
        ([("TradeExecuted", trade(1, "o1", "o2", 1, (0, 0)))], "not in the book"),
        (
            [
                ("OrderAccepted", accepted(1, "o1", "BUY", 5, 100)),
                ("OrderAccepted", accepted(2, "o2", "SELL", 5, 100)),
                ("TradeExecuted", trade(3, "o1", "o2", 2, (2, 3))),
            ],
            "do not add up",
        ),
        (
            [
                ("OrderAccepted", accepted(1, "o1", "BUY", 5, 100)),
                ("OrderCancelled", {"sequence": 2, "order_id": "o1", "cancelled_quantity": 4}),
            ],
            "does not match",
        ),
        ([("OrderAmended", {"sequence": 1})], "unknown event type"),
    ],
)
def test_history_that_does_not_fit_together_is_refused(
    events: list[tuple[str, Any]], message: str
) -> None:
    book = ReplayBook("CALVERA")
    with pytest.raises(CorruptHistoryError, match=message):
        for event_type, data in events:
            book.apply(event_type, data, T)


def test_there_is_no_hash_while_a_market_order_is_half_matched() -> None:
    book = ReplayBook("CALVERA")
    book.apply("OrderAccepted", accepted(1, "m1", "BUY", 5, None), T)
    with pytest.raises(CorruptHistoryError, match="market order"):
        book.state_hash()
