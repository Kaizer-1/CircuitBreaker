"""The book's memory of finished orders stays bounded (ADR-0017 note "A memory of one hour").

Found on kind (2026-11-14): with every finished order kept for ever, about 600,000 orders after a
few load tests pushed the engine past its 256 MiB limit and it was killed at every start-up.
"""

import tracemalloc

from matching_engine.domain.book import OrderBook
from matching_engine.domain.model import (
    CLOSED_MEMORY_MS,
    BookSnapshot,
    OrderType,
    PlaceOrder,
    Side,
    ms_of,
    timestamp_of,
)
from tests.builders import ALICE, BOB, id_source, oid

TICKER = "CALVERA"
START = ms_of("2026-11-20T06:00:00.000Z")
HOUR = 3_600_000


def order(n: int, trader: str, side: str, at: int) -> PlaceOrder:
    return PlaceOrder(
        command_id=f"c5c5c5c5-0000-4000-8000-{n:012d}",
        correlation_id=f"req-{n}",
        ticker=TICKER,
        order_id=oid(n),
        trader_id=trader,
        side=Side(side),
        order_type=OrderType.LIMIT,
        quantity=10,
        limit_price_paise=24550,
        protection_price_paise=None,
        placed_at=timestamp_of(at),
    )


def test_memory_stays_flat_over_four_simulated_hours() -> None:
    """Two orders a second meet and fill each other: 28,800 finished orders over four hours."""
    book = OrderBook(TICKER)
    new_id = id_source()
    remembered, traced = [], []
    tracemalloc.start()
    try:
        for second in range(4 * 3600):
            at = START + second * 1000
            for n, trader, side in ((2 * second + 1, ALICE, "SELL"), (2 * second + 2, BOB, "BUY")):
                for event in book.decide(
                    order(n, trader, side, at), new_id=new_id, now=timestamp_of(at)
                ).events:
                    book.apply(event)
            if (second + 1) % 1800 == 0:  # every half hour
                remembered.append(book.remembered_count())
                traced.append(tracemalloc.get_traced_memory()[0])
    finally:
        tracemalloc.stop()
    # One hour holds 7,200 finished orders; after the first hour the count no longer grows.
    assert remembered[:2] == [3_600, 7_200]
    assert all(7_200 <= r <= 7_202 for r in remembered[2:]), remembered
    assert max(traced[2:]) < traced[1] * 1.10, traced  # memory flat within 10% after hour one
    assert len(book.snapshot().closed) <= 7_202


def test_a_snapshot_with_a_huge_closed_set_is_forgotten_on_time_after_loading() -> None:
    """A snapshot stored before closing times existed (here 200,000 orders, as on kind).

    Its orders count as closed at the snapshot's moment: all remembered for one more hour, then
    forgotten together by the first event after it; a late copy of one of them is refused.
    """
    as_of = START
    closed = [[oid(n), "FILLED", ALICE] for n in range(1, 200_001)]
    snapshot = BookSnapshot.from_dict(
        {
            "ticker": TICKER,
            "sequence": 400_000,
            "as_of": timestamp_of(as_of),
            "bids": [],
            "asks": [],
            "closed": closed,
        }
    )
    book = OrderBook.from_snapshot(snapshot)
    assert book.remembered_count() == 200_000 and book.forget_before is None
    new_id = id_source()

    def step(n: int, at: int) -> None:
        for event in book.decide(
            order(n, BOB, "BUY", at), new_id=new_id, now=timestamp_of(at)
        ).events:
            book.apply(event)

    step(300_001, as_of + HOUR - 1_000)  # one second short of the hour: all still remembered
    assert book.remembered_count() == 200_000 and book.status_of(oid(1)) == "FILLED"
    step(300_002, as_of + HOUR + 1)
    assert book.remembered_count() == 0 and book.status_of(oid(1)) is None
    copy = order(1, ALICE, "SELL", as_of - 60_000)  # a late copy of a forgotten order
    assert book.decide(copy, new_id=new_id, now=timestamp_of(as_of + HOUR + 2)).events == ()


def test_a_snapshot_is_pruned_on_load_to_its_forget_before() -> None:
    """Orders that closed before forget_before are dropped when the snapshot is loaded."""
    edge = START + CLOSED_MEMORY_MS
    closed = [
        [oid(n), "CANCELLED", ALICE, timestamp_of(START + n * 1000)] for n in range(1, 7_201)
    ]  # one every second for two hours: the first hour is before forget_before
    snapshot = BookSnapshot.from_dict(
        {
            "ticker": TICKER,
            "sequence": 9_000,
            "as_of": timestamp_of(START + 2 * HOUR),
            "bids": [],
            "asks": [],
            "closed": closed,
            "forget_before": timestamp_of(edge),
        }
    )
    book = OrderBook.from_snapshot(snapshot)
    kept = [c[0] for c in book.snapshot().closed]
    assert kept == sorted(oid(n) for n in range(3_600, 7_201))  # closed at or after the edge
    assert book.remembered_count() == len(kept)
    assert book.status_of(oid(3_599)) is None and book.status_of(oid(3_600)) == "CANCELLED"
