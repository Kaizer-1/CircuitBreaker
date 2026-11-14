"""The book remembers finished orders for one hour of event time (ADR-0017 note "A memory of one
hour"), and forgetting never lets a late or duplicate placement trade.

Workloads here move the clock: steps are seconds to 90 minutes apart (and sometimes back, as a
wall clock can be corrected), placements reach the engine
late or twice, and expiries arrive before the placement they expire. A small model of what the
engine has seen says, for every placement, whether it may be accepted; the book must agree, and a
book restored from a snapshot at any step must make the same decisions and have the same hash.
"""

from dataclasses import dataclass

from hypothesis import given
from hypothesis import strategies as st

from matching_engine.domain.book import OrderBook
from matching_engine.domain.model import (
    CLOSED_MEMORY_MS,
    CancelOrder,
    Command,
    ExpireOrder,
    OrderAccepted,
    OrderExpired,
    OrderType,
    PlaceOrder,
    Side,
    TradeExecuted,
    ms_of,
    timestamp_of,
)
from tests.builders import ALICE, BOB, CARA, id_source, oid

TICKER = "CALVERA"
START = ms_of("2026-11-20T09:00:00.000Z")
TRADERS = (ALICE, BOB, CARA)


@dataclass(frozen=True)
class Place:
    trader: int
    side: str
    ticks: int  # from 24550; small, so orders often trade
    delay_ms: int  # how long the OrderPlaced took to reach the engine
    delivered: bool  # False: still in flight (an expiry or a late copy may come first)


@dataclass(frozen=True)
class Copy:
    """The OrderPlaced of an earlier order arrives (again): a late or duplicate delivery."""

    ref: int


@dataclass(frozen=True)
class Expire:
    ref: int


@dataclass(frozen=True)
class Cancel:
    ref: int


Action = Place | Copy | Expire | Cancel

_gap = st.one_of(
    st.integers(0, 2_000),  # milliseconds apart: most steps
    st.integers(-3_000, -1),  # the engine's clock stepped back (closing times out of order)
    st.integers(1_000, 600_000),  # up to 10 minutes
    st.integers(3_000_000, 5_400_000),  # 50 to 90 minutes: across the memory's edge
    st.integers(CLOSED_MEMORY_MS - 3_000, CLOSED_MEMORY_MS + 3_000),  # the edge itself
)
_action = st.one_of(
    st.builds(
        Place,
        trader=st.integers(0, len(TRADERS) - 1),
        side=st.sampled_from(["BUY", "SELL"]),
        ticks=st.integers(-3, 3),
        delay_ms=st.integers(0, 70_000),
        delivered=st.booleans(),
    ),
    st.builds(Copy, ref=st.integers(0, 100)),
    st.builds(Expire, ref=st.integers(0, 100)),
    st.builds(Cancel, ref=st.integers(0, 100)),
)
workloads = st.lists(st.tuples(_gap, _action), min_size=1, max_size=50)
restore_every = st.integers(1, 7)


@dataclass
class Seen:
    """What the engine has been told about each order: the model the book is checked against."""

    placed: dict[str, PlaceOrder]
    known: set[str]  # accepted or tombstoned: any later OrderPlaced for it must not trade


def play(workload: list[tuple[int, Action]], every: int) -> None:
    book, restored = OrderBook(TICKER), OrderBook(TICKER)
    new_id, other_id = id_source(), id_source()
    seen = Seen({}, set())
    orders: list[str] = []
    clock, newest_event = START, None
    commands = 0
    for gap, action in workload:
        clock += gap
        command = build(action, clock, orders, seen)
        if command is None:
            continue
        commands += 1
        now = timestamp_of(clock)
        decision = book.decide(command, new_id=new_id, now=now)
        twin = restored.decide(command, new_id=other_id, now=now)
        assert (twin.outcome, len(twin.events)) == (decision.outcome, len(decision.events))

        if isinstance(command, PlaceOrder):
            accepted = any(isinstance(e, OrderAccepted) for e in decision.events)
            forget_before = None if newest_event is None else newest_event - CLOSED_MEMORY_MS
            may_trade = command.order_id not in seen.known and (
                forget_before is None or ms_of(command.placed_at) >= forget_before
            )
            assert accepted == may_trade, (command.order_id, accepted, may_trade)
            if not may_trade:
                assert decision.events == ()  # refused: nothing at all, so nothing can trade
        for event in decision.events:
            if isinstance(event, OrderAccepted):
                seen.known.add(event.order_id)
            elif isinstance(event, TradeExecuted):
                assert event.buy_order_id in seen.known and event.sell_order_id in seen.known
            elif isinstance(event, OrderExpired):
                seen.known.add(event.order_id)  # the tombstone
            book.apply(event)
        for event in twin.events:
            restored.apply(event)
        if decision.events:
            newest_event = max(newest_event or clock, clock)
        assert restored.state_hash() == book.state_hash()
        if commands % every == 0:  # a restart: the twin continues from a snapshot of itself
            restored = OrderBook.from_snapshot(restored.snapshot())
            assert restored.state_hash() == book.state_hash()


def build(action: Action, clock: int, orders: list[str], seen: Seen) -> Command | None:
    if isinstance(action, Place):
        n = len(orders) + 1
        order_id = oid(n)
        orders.append(order_id)
        place = PlaceOrder(
            command_id=f"c1c1c1c1-0000-4000-8000-{n:012d}",
            correlation_id=f"req-{n}",
            ticker=TICKER,
            order_id=order_id,
            trader_id=TRADERS[action.trader],
            side=Side(action.side),
            order_type=OrderType.LIMIT,
            quantity=10,
            limit_price_paise=24550 + 5 * action.ticks,
            protection_price_paise=None,
            placed_at=timestamp_of(clock - action.delay_ms),
        )
        seen.placed[order_id] = place
        return place if action.delivered else None
    if not orders:
        return None
    order_id = orders[action.ref % len(orders)]
    place = seen.placed[order_id]
    common = {
        "command_id": f"c2c2c2c2-0000-4000-8000-{clock % 10**12:012d}",
        "correlation_id": "req-x",
        "ticker": TICKER,
        "order_id": order_id,
        "trader_id": place.trader_id,
    }
    if isinstance(action, Copy):
        return PlaceOrder(
            **common,
            side=place.side,
            order_type=place.order_type,
            quantity=place.quantity,
            limit_price_paise=place.limit_price_paise,
            protection_price_paise=None,
            placed_at=place.placed_at,  # a copy keeps its order's placement time
        )
    if isinstance(action, Expire):
        return ExpireOrder(**common, requested_at=timestamp_of(clock))
    return CancelOrder(**common, requested_at=timestamp_of(clock))


@given(workloads, restore_every)
def test_no_late_or_duplicate_placement_ever_trades_and_restores_agree(
    workload: list[tuple[int, Action]], every: int
) -> None:
    play(workload, every)


def test_a_copy_arriving_after_its_order_is_forgotten_is_refused() -> None:
    """The case the age rule exists for: the ID is gone from memory, the placement is not new."""
    book = OrderBook(TICKER)
    new_id = id_source()
    t0 = START
    first = place_at(1, ALICE, "SELL", t0)
    for event in book.decide(first, new_id=new_id, now=timestamp_of(t0)).events:
        book.apply(event)
    buyer = place_at(2, BOB, "BUY", t0 + 1_000)
    for event in book.decide(buyer, new_id=new_id, now=timestamp_of(t0 + 1_000)).events:
        book.apply(event)  # both filled: order 1 closed at t0 + 1 s
    later = t0 + CLOSED_MEMORY_MS + 5_000
    tick = place_at(3, CARA, "BUY", later, price=24000)  # any event moves forget_before on
    for event in book.decide(tick, new_id=new_id, now=timestamp_of(later)).events:
        book.apply(event)
    assert book.status_of(oid(1)) is None  # forgotten
    copy = place_at(1, ALICE, "SELL", t0, command="c3c3c3c3-0000-4000-8000-000000000001")
    decision = book.decide(copy, new_id=new_id, now=timestamp_of(later + 1))
    assert decision.events == () and decision.detail == "placed before forget_before: never trades"


def place_at(
    n: int, trader: str, side: str, at: int, *, price: int = 24550, command: str | None = None
) -> PlaceOrder:
    return PlaceOrder(
        command_id=command or f"c4c4c4c4-0000-4000-8000-{n:012d}",
        correlation_id=f"req-{n}",
        ticker=TICKER,
        order_id=oid(n),
        trader_id=trader,
        side=Side(side),
        order_type=OrderType.LIMIT,
        quantity=10,
        limit_price_paise=price,
        protection_price_paise=None,
        placed_at=timestamp_of(at),
    )


def apply_all(book: OrderBook, command: Command, at: int) -> None:
    for event in book.decide(command, new_id=id_source("e5e5e5e5"), now=timestamp_of(at)).events:
        book.apply(event)


def test_forget_before_never_moves_back_when_the_clock_does() -> None:
    book = OrderBook(TICKER)
    edge = START + CLOSED_MEMORY_MS
    apply_all(book, place_at(1, ALICE, "BUY", START, price=24000), START)
    apply_all(book, place_at(2, ALICE, "BUY", edge + 10_000, price=24000), edge + 10_000)
    assert book.forget_before == timestamp_of(START + 10_000)
    # The clock is corrected 5 seconds back: the memory's start stays where it was.
    apply_all(book, place_at(3, ALICE, "BUY", edge + 5_000, price=24000), edge + 5_000)
    assert book.status_of(oid(3)) == "RESTING"
    assert book.forget_before == timestamp_of(START + 10_000)
    late = place_at(4, BOB, "SELL", START + 7_000)  # after the corrected edge, before the real one
    decision = book.decide(late, new_id=id_source(), now=timestamp_of(edge + 6_000))
    assert decision.events == ()


def test_an_order_closed_out_of_time_order_is_forgotten_on_time() -> None:
    """Closing times can run backwards (a corrected clock): forgetting does not depend on that."""
    book = OrderBook(TICKER)
    apply_all(book, place_at(1, ALICE, "SELL", START), START)
    apply_all(book, place_at(2, BOB, "BUY", START + 5_000), START + 5_000)  # 1 and 2 close at +5 s
    apply_all(book, place_at(3, ALICE, "SELL", START + 2_000), START + 2_000)  # clock back
    apply_all(book, place_at(4, BOB, "BUY", START + 2_000), START + 2_000)  # 3 and 4 close at +2 s
    apply_all(
        book,
        place_at(5, CARA, "BUY", START + CLOSED_MEMORY_MS + 3_000, price=24000),
        START + CLOSED_MEMORY_MS + 3_000,
    )
    # forget_before is START + 3 s: orders 3 and 4 are forgotten although 1 and 2, older in the
    # book's memory, are still remembered.
    assert [book.status_of(oid(n)) for n in (1, 2, 3, 4)] == ["FILLED", "FILLED", None, None]
    assert [c[0] for c in book.snapshot().closed] == [oid(1), oid(2)]
    assert OrderBook.from_snapshot(book.snapshot()).state_hash() == book.state_hash()
    # A forgotten order is expired again (a late expiry request): the new tombstone stays when the
    # old closing of the same ID leaves the book's memory.
    expire = ExpireOrder(
        command_id="c6c6c6c6-0000-4000-8000-000000000001",
        correlation_id="req-x",
        ticker=TICKER,
        order_id=oid(3),
        trader_id=ALICE,
        requested_at=timestamp_of(START + CLOSED_MEMORY_MS + 4_000),
    )
    apply_all(book, expire, START + CLOSED_MEMORY_MS + 4_000)
    apply_all(
        book,
        place_at(6, CARA, "BUY", START + CLOSED_MEMORY_MS + 6_000, price=24000),
        START + CLOSED_MEMORY_MS + 6_000,
    )
    assert [book.status_of(oid(n)) for n in (1, 2, 3)] == [None, None, "EXPIRED"]
