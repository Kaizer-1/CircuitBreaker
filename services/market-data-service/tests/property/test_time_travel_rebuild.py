"""Property: the market rebuilt at a random moment T equals the live market recorded at T.

A reference exchange (tests/fakes.py) runs a random workload: orders, market orders, cancels,
expiry tombstones and cancel rejections, with time moving on by random steps (sometimes not at
all, so several commands share one millisecond). Like the engine, it takes a snapshot every few
events. After every command the test records the live hash and the time. Then, for random T
(exact event times, times in between, before the first event, after the last), time travel
rebuilds the book from the nearest snapshot at or before T plus the events up to T, and its hash
must equal the hash recorded at T. The exchange and the ReplayBook hash independently.
"""

import asyncio
from bisect import bisect_right

from hypothesis import given
from hypothesis import strategies as st

from market_data_service.application.time_travel import TimeTravel
from tests.fakes import Exchange, FakeEventStore, InMemoryMarketStore, ms

TRADERS = ["a", "b", "c"]
Command = tuple[str, str, str, int, int | None, int]  # kind, trader, side, qty, price, wait ms
command = st.tuples(
    st.sampled_from(["place"] * 6 + ["cancel", "expire", "reject"]),
    st.sampled_from(TRADERS),
    st.sampled_from(["BUY", "SELL"]),
    st.integers(min_value=1, max_value=40),
    st.one_of(st.none(), st.integers(min_value=96, max_value=104).map(lambda p: p * 5)),
    st.sampled_from([0, 0, 1, 7, 250, 1_000, 61_000]),
)


def run(exchange: Exchange, cmd: Command) -> None:
    kind, trader, side, quantity, price, wait = cmd
    exchange.advance(wait / 1000)
    if kind == "place":
        exchange.place(trader, side, quantity, price)
    elif kind == "cancel":
        exchange.cancel_oldest(side)
    elif kind == "expire":
        exchange.expire_unseen(trader)
    else:
        exchange.reject_cancel(trader)


@given(
    commands=st.lists(command, min_size=1, max_size=60),
    snapshot_every=st.integers(min_value=1, max_value=12),
    probes=st.lists(st.integers(min_value=-2_000, max_value=2_000), min_size=1, max_size=8),
    picks=st.lists(st.integers(min_value=0, max_value=10_000), min_size=1, max_size=8),
)
def test_the_market_rebuilt_at_any_moment_equals_the_live_market_then(
    commands: list[Command], snapshot_every: int, probes: list[int], picks: list[int]
) -> None:
    exchange, events = Exchange(), FakeEventStore()
    events.events["CALVERA"] = exchange.log
    events.history["CALVERA"] = []
    empty_hash = exchange.state_hash()
    recorded: list[tuple[int, str]] = []  # (time of the last event, live hash), in order
    last_snapshot = 0
    for cmd in commands:
        run(exchange, cmd)
        if exchange.as_of is None:
            continue
        recorded.append((ms(exchange.as_of), exchange.state_hash()))
        if exchange.sequence - last_snapshot >= snapshot_every:  # the engine's rule (P-03)
            events.history["CALVERA"].append(exchange.snapshot())
            last_snapshot = exchange.sequence
    time_travel = TimeTravel(events, InMemoryMarketStore())
    times = [t for t, _ in recorded]
    first = ms(exchange.log[0].occurred_at) if exchange.log else ms("2026-11-20T10:42:00Z")
    moments = [first + p for p in probes] + [times[i % len(times)] for i in picks if times]

    for at in moments:
        live = recorded[bisect_right(times, at) - 1][1] if bisect_right(times, at) else empty_hash
        book = asyncio.run(time_travel.rebuild("CALVERA", at))
        assert book.state_hash() == live, f"rebuild at {at} (sequence {book.sequence})"


def test_a_rebuild_reads_only_the_events_after_the_nearest_snapshot() -> None:
    exchange, events = Exchange(), FakeEventStore()
    events.events["CALVERA"] = exchange.log
    for n in range(40):
        exchange.place(TRADERS[n % 3], "BUY" if n % 2 else "SELL", 5, 500 + 5 * (n % 3))
        exchange.advance(1)
        if n == 30:
            events.history["CALVERA"] = [exchange.snapshot()]
    snap = events.history["CALVERA"][0]
    applied: list[int] = []

    class Counting(FakeEventStore):
        async def events_after(self, ticker, after_sequence, *, until_ms=None):  # type: ignore[no-untyped-def]
            async for e in super().events_after(ticker, after_sequence, until_ms=until_ms):
                applied.append(int(e.data["sequence"]))
                yield e

    counting = Counting()
    counting.events, counting.history = events.events, events.history
    assert exchange.as_of is not None
    book = asyncio.run(
        TimeTravel(counting, InMemoryMarketStore()).rebuild("CALVERA", ms(exchange.as_of))
    )
    assert book.state_hash() == exchange.state_hash()
    assert applied and applied[0] == snap.sequence + 1  # nothing before the snapshot is read
