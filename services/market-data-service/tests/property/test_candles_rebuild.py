"""Property: candles rebuilt from the full trade stream equal the live candles.

Live candles see trades and clock ticks interleaved at random, with the clock sometimes far
ahead of the trades (so trades arrive "late" for buckets the clock already closed, as after an
engine crash). Whatever the interleaving, both the candles in memory and the ones written to the
store (every candle the series reported as changed, last write wins) equal `rebuild`.
"""

from hypothesis import given
from hypothesis import strategies as st

from market_data_service.domain.candles import INTERVALS, Candle, CandleSeries, rebuild

T0 = 1_795_000_200_000
REF = 24550

trade = st.tuples(
    st.just("trade"),
    st.integers(min_value=0, max_value=12_000),  # ms after the previous trade (never backwards)
    st.integers(min_value=4000, max_value=5000).map(lambda p: p * 5),
    st.integers(min_value=1, max_value=500),
)
tick = st.tuples(
    st.just("tick"),
    st.integers(min_value=0, max_value=40_000),  # ms the clock runs ahead of the last trade
    st.just(0),
    st.just(0),
)


@given(
    steps=st.lists(st.one_of(trade, tick), max_size=60),
    interval=st.sampled_from(sorted(INTERVALS)),
)
def test_live_and_stored_candles_equal_a_rebuild_from_the_trades(
    steps: list[tuple[str, int, int, int]], interval: str
) -> None:
    ms = INTERVALS[interval]
    series = CandleSeries(ms, T0, REF)
    stored: dict[int, Candle] = {}
    trades: list[tuple[int, int, int]] = []
    event_time = clock = T0
    for kind, delta, price, quantity in steps:
        if kind == "trade":
            event_time += delta
            trades.append((price, quantity, event_time))
            for candle, _ in series.add_trade(price, quantity, event_time):
                stored[candle.bucket_start_ms] = candle
        else:
            clock = max(clock, event_time + delta)  # the clock never goes back
            for candle in series.close_until(clock):
                stored[candle.bucket_start_ms] = candle

    last = series.forming_start_ms
    expected = rebuild(trades, ms, T0, last, REF)
    assert series.candles(T0, last) == expected
    closed = [c for c in expected if c.bucket_start_ms < last]
    assert [stored[c.bucket_start_ms] for c in closed] == closed
    # Every bucket from the start exists: no gaps for charts or bots.
    assert [c.bucket_start_ms for c in expected] == list(range(T0, last + 1, ms))


@given(steps=st.lists(st.one_of(trade, tick), max_size=40))
def test_every_closed_candle_is_reported_and_reported_again_only_when_it_changed(
    steps: list[tuple[str, int, int, int]],
) -> None:
    ms = INTERVALS["5s"]
    series = CandleSeries(ms, T0, REF)
    reported: dict[int, list[Candle]] = {}
    event_time = clock = T0
    for kind, delta, price, quantity in steps:
        if kind == "trade":
            event_time += delta
            changes = series.add_trade(price, quantity, event_time)
            closed = [c for c, is_closed in changes if is_closed]
        else:
            clock = max(clock, event_time + delta)
            closed = series.close_until(clock)
        for c in closed:
            reported.setdefault(c.bucket_start_ms, []).append(c)
    assert sorted(reported) == list(range(T0, series.forming_start_ms, ms))
    for versions in reported.values():
        # A bucket is reported again only with a correction (it may return to an earlier value).
        assert all(a != b for a, b in zip(versions, versions[1:], strict=False))
