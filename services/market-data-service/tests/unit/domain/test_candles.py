"""Candles: bucket boundaries, gaps with no trades, OHLC correctness, late trades."""

from market_data_service.domain.candles import INTERVALS, Candle, CandleSeries, bucket_of, rebuild

S5 = INTERVALS["5s"]
M1 = INTERVALS["1m"]
T = 1_795_000_200_000  # a multiple of 5 minutes, so every interval's bucket starts here
REF = 24550


def flat(bucket: int, price: int) -> Candle:
    return Candle(bucket, price, price, price, price, 0, 0)


def test_buckets_are_aligned_to_the_epoch_and_half_open() -> None:
    assert T % INTERVALS["5m"] == 0
    assert bucket_of(T, S5) == T
    assert bucket_of(T + 4_999, S5) == T  # the last millisecond of [T, T + 5 s)
    assert bucket_of(T + 5_000, S5) == T + 5_000  # the end belongs to the next bucket
    assert bucket_of(T + 59_999, M1) == T and bucket_of(T + 60_000, M1) == T + M1


def test_open_high_low_close_volume_and_count_follow_the_trades_in_order() -> None:
    s = CandleSeries(S5, T, REF)
    for price, qty, at in [
        (24600, 10, T + 100),
        (24700, 5, T + 200),
        (24500, 1, T + 300),
        (24650, 4, T + 4_999),
    ]:
        s.add_trade(price, qty, at)
    assert s.forming() == Candle(T, 24600, 24700, 24500, 24650, 20, 4)


def test_a_trade_exactly_on_the_boundary_starts_the_next_candle_and_closes_this_one() -> None:
    s = CandleSeries(S5, T, REF)
    s.add_trade(24600, 10, T + 1_000)
    changed = s.add_trade(24610, 3, T + 5_000)
    assert changed == [
        (Candle(T, 24600, 24600, 24600, 24600, 10, 1), True),
        (Candle(T + S5, 24610, 24610, 24610, 24610, 3, 1), False),
    ]


def test_a_gap_with_no_trades_is_filled_with_flat_candles_at_the_previous_close() -> None:
    s = CandleSeries(S5, T, REF)
    s.add_trade(24600, 10, T + 1_000)
    s.add_trade(24400, 10, T + 2_000)
    changed = s.add_trade(24800, 1, T + 3 * S5 + 10)
    closed = [c for c, is_closed in changed if is_closed]
    assert closed == [
        Candle(T, 24600, 24600, 24400, 24400, 20, 2),
        flat(T + S5, 24400),
        flat(T + 2 * S5, 24400),
    ]


def test_before_the_first_trade_candles_are_flat_at_the_reference_price() -> None:
    s = CandleSeries(S5, T, REF)
    assert s.close_until(T + 3 * S5) == [flat(T, REF), flat(T + S5, REF), flat(T + 2 * S5, REF)]
    assert s.forming() == flat(T + 3 * S5, REF)


def test_the_clock_closes_only_buckets_that_have_ended() -> None:
    s = CandleSeries(S5, T, REF)
    assert s.close_until(T + 4_999) == []
    assert s.close_until(T + 5_000) == [flat(T, REF)]
    assert s.close_until(T + 5_000) == []  # closing twice changes nothing


def test_a_late_trade_corrects_its_closed_candle_and_the_flat_ones_after_it() -> None:
    s = CandleSeries(S5, T, REF)
    s.add_trade(24600, 10, T + 1_000)
    s.close_until(T + 3 * S5)  # the clock closed [T+5s, T+15s) as flat at 24600
    # The engine republishes a trade from T + 7 s after a crash.
    changed = s.add_trade(24900, 2, T + S5 + 2_000)
    assert changed == [
        (Candle(T + S5, 24900, 24900, 24900, 24900, 2, 1), True),
        (flat(T + 2 * S5, 24900), True),
        (flat(T + 3 * S5, 24900), False),  # the forming one too
    ]


def test_a_correction_stops_at_the_next_candle_with_trades_of_its_own() -> None:
    s = CandleSeries(S5, T, REF)
    s.add_trade(24600, 1, T)
    s.close_until(T + S5)
    s.add_trade(24700, 1, T + S5)  # bucket 1 has its own trade
    s.close_until(T + 3 * S5)
    changed = s.add_trade(24800, 1, T + 4_000)  # late, into bucket 0
    assert [c.bucket_start_ms for c, _ in changed] == [T]


def test_restore_continues_from_stored_candles() -> None:
    stored = [Candle(T, 24600, 24700, 24500, 24650, 20, 4), flat(T + S5, 24650)]
    s = CandleSeries.restore(S5, stored)
    assert s.since_ms == T and s.forming_start_ms == T + S5
    assert s.close_until(T + 3 * S5) == [flat(T + S5, 24650), flat(T + 2 * S5, 24650)]
    s.add_trade(24000, 1, T + 3 * S5)
    assert s.forming() == Candle(T + 3 * S5, 24000, 24000, 24000, 24000, 1, 1)


def test_only_a_window_of_candles_is_kept_in_memory_and_prices_carry_over() -> None:
    s = CandleSeries(S5, T, REF, keep=3)
    s.add_trade(24600, 1, T)
    s.close_until(T + 10 * S5)
    assert s.since_ms == T + 8 * S5  # 3 buckets kept: 8, 9 and the forming 10
    assert s.candles(T, T + 10 * S5)[0] == flat(T + 8 * S5, 24600)
    assert s.add_trade(1, 1, T + 1) == []  # older than the window: nothing to correct


def test_candles_returns_a_window_with_the_forming_candle() -> None:
    s = CandleSeries(M1, T, REF)
    s.close_until(T + 2 * M1)
    assert [c.bucket_start_ms for c in s.candles(T - M1, T + 10 * M1)] == [T, T + M1, T + 2 * M1]


def test_rebuild_from_trades_alone() -> None:
    trades = [(24600, 10, T + 1_000), (24400, 5, T + 2_000), (24800, 1, T + 2 * S5)]
    assert rebuild(trades, S5, T, T + 3 * S5, REF) == [
        Candle(T, 24600, 24600, 24400, 24400, 15, 2),
        flat(T + S5, 24400),
        Candle(T + 2 * S5, 24800, 24800, 24800, 24800, 1, 1),
        flat(T + 3 * S5, 24800),
    ]
