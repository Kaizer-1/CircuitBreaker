"""Property: depth built from matching.v1 equals the engine's book, and is never crossed.

A reference exchange (tests/fakes.py) decides random orders and cancels like the engine and
emits its events. After every event the published depth is never crossed (not even in the
middle of a match); after every command it equals the exchange's own book, level by level.
It also holds when the projection starts from a snapshot taken half-way.
"""

from hypothesis import given
from hypothesis import strategies as st

from market_data_service.domain.depth import DepthBook
from tests.fakes import Exchange

TRADERS = ["a", "b", "c"]
Command = tuple[str, str, str, int, int | None]  # kind, trader, side, quantity, price
command = st.one_of(
    st.tuples(
        st.just("place"),
        st.sampled_from(TRADERS),
        st.sampled_from(["BUY", "SELL"]),
        st.integers(min_value=1, max_value=50),
        st.one_of(st.none(), st.integers(min_value=95, max_value=105).map(lambda p: p * 5)),
    ),
    st.tuples(
        st.just("cancel"), st.just(""), st.sampled_from(["BUY", "SELL"]), st.just(0), st.none()
    ),
)


def levels(book: DepthBook) -> tuple[list[tuple[int, int, int]], list[tuple[int, int, int]]]:
    bids, asks = book.depth(1_000)
    return (
        [(lv.price_paise, lv.quantity, lv.order_count) for lv in bids],
        [(lv.price_paise, lv.quantity, lv.order_count) for lv in asks],
    )


def never_crossed(book: DepthBook) -> None:
    bids, asks = book.depth(1)
    if bids and asks:
        assert bids[0].price_paise < asks[0].price_paise


def run(exchange: Exchange, book: DepthBook, cmd: Command) -> None:
    kind, trader, side, quantity, price = cmd
    events = (
        exchange.place(trader, side, quantity, price)
        if kind == "place"
        else exchange.cancel_oldest(side)
    )
    for event in events:
        book.apply(event.event_type, event.data)
        never_crossed(book)
    assert levels(book) == exchange.depth()


@given(commands=st.lists(command, max_size=80))
def test_depth_from_events_equals_the_book_and_is_never_crossed(
    commands: list[Command],
) -> None:
    exchange, book = Exchange(), DepthBook()
    for cmd in commands:
        run(exchange, book, cmd)


@given(before=st.lists(command, max_size=40), after=st.lists(command, max_size=40))
def test_depth_from_a_snapshot_plus_later_events_equals_the_book(
    before: list[Command], after: list[Command]
) -> None:
    exchange = Exchange()
    for cmd in before:
        _decide(exchange, cmd)
    snap = exchange.snapshot()
    book = DepthBook.from_snapshot(snap.bids, snap.asks)
    assert levels(book) == exchange.depth()
    for cmd in after:
        run(exchange, book, cmd)


def _decide(exchange: Exchange, cmd: Command) -> None:
    kind, trader, side, quantity, price = cmd
    if kind == "place":
        exchange.place(trader, side, quantity, price)
    else:
        exchange.cancel_oldest(side)
