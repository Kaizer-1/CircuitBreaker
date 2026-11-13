"""docs/how-matching-works.md, step by step: the walkthrough and the engine agree."""

from matching_engine.domain.book import OrderBook
from matching_engine.domain.model import BookSnapshot, Command, MarketEvent, Outcome, PriceLevel
from tests.builders import T0, cancel, expire, id_source, limit, market, oid

RAJU, MEERA = "3d2a1b0c-9e8f-4a7b-8c6d-5e4f3a2b1c0d", "5e6f7a8b-9c0d-4e1f-8a2b-3c4d5e6f7a8b"
BOT01 = "b0b0b0b0-0001-4000-8000-000000000001"
BOT03 = "b0b0b0b0-0003-4000-8000-000000000003"
BOT05 = "b0b0b0b0-0005-4000-8000-000000000005"
BOT07 = "b0b0b0b0-0007-4000-8000-000000000007"


def book_before() -> OrderBook:
    return OrderBook.from_snapshot(
        BookSnapshot(
            ticker="CALVERA",
            sequence=1040,
            as_of=T0,
            bids=((24500, ((oid(101), BOT01, 40, 988),)), (24450, ((oid(102), MEERA, 10, 1002),))),
            asks=((24550, ((oid(103), BOT03, 20, 995),)), (24650, ((oid(104), BOT05, 15, 1010),))),
            closed=(),
        )
    )


def step(book: OrderBook, command: Command) -> list[tuple[int, str, dict[str, object]]]:
    decision = book.decide(command, new_id=id_source(), now=T0)
    for event in decision.events:
        book.apply(event)
    return [(e.sequence, e.event_type, summary(e)) for e in decision.events]


FIELDS = (
    "price_paise",
    "quantity",
    "buy_order_remaining_quantity",
    "sell_order_remaining_quantity",
    "cancelled_quantity",
    "reason",
)


def summary(e: MarketEvent) -> dict[str, object]:
    found = {k: getattr(e, k) for k in FIELDS if hasattr(e, k)}
    return {k: (str(v) if k == "reason" else v) for k, v in found.items()}


def trade(price: int, quantity: int, buy_left: int, sell_left: int) -> dict[str, object]:
    return {
        "price_paise": price,
        "quantity": quantity,
        "buy_order_remaining_quantity": buy_left,
        "sell_order_remaining_quantity": sell_left,
    }


def levels(side: list[PriceLevel]) -> list[tuple[int, list[tuple[str, int, int]]]]:
    return [
        (
            lv.price_paise,
            [(o.trader_id, o.remaining_quantity, o.accepted_sequence) for o in lv.orders],
        )
        for lv in side
    ]


def test_the_worked_example_in_how_matching_works() -> None:
    book = book_before()
    assert (book.best_bid(), book.best_ask()) == (24500, 24550)

    # 1. Raju: BUY 50 LIMIT 24600
    assert step(book, limit(1, RAJU, "BUY", 50, 24600)) == [
        (1041, "OrderAccepted", {"quantity": 50}),
        (1042, "TradeExecuted", trade(24550, 20, 30, 0)),
    ]
    assert book.best_bid() == 24600

    # 2. Bot-07: SELL 40 MARKET, protection 22095
    assert step(book, market(2, BOT07, "SELL", 40, 22095)) == [
        (1043, "OrderAccepted", {"quantity": 40}),
        (1044, "TradeExecuted", trade(24600, 30, 0, 10)),
        (1045, "TradeExecuted", trade(24500, 10, 30, 0)),
    ]

    # 3. Bot-01: SELL 60 LIMIT 24450, whose best match is Bot-01's own bid
    assert step(book, limit(3, BOT01, "SELL", 60, 24450)) == [
        (1046, "OrderAccepted", {"quantity": 60}),
        (1047, "OrderCancelled", {"cancelled_quantity": 60, "reason": "SELF_TRADE_PREVENTED"}),
    ]

    # 4. Meera cancels her bid
    assert step(book, cancel(102, MEERA)) == [
        (1048, "OrderCancelled", {"cancelled_quantity": 10, "reason": "USER_REQUESTED"}),
    ]

    # 5. An expiry for an order the engine never saw, then a late copy of it
    assert step(book, expire(9, RAJU)) == [(1049, "OrderExpired", {})]
    late = book.decide(limit(9, RAJU, "BUY", 5, 24700), new_id=id_source(), now=T0)
    assert (late.events, late.outcome) == ((), Outcome.REFUSED)

    # The book after
    assert levels(book.bids()) == [(24500, [(BOT01, 30, 988)])]
    assert levels(book.asks()) == [(24650, [(BOT05, 15, 1010)])]
    assert book.sequence == 1049
