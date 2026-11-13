"""Time travel (Q-12): candles, trades and the last price as they were at T, nothing after T."""

import asyncio
from datetime import UTC, datetime

from market_data_service.application.time_travel import MarketAt, TimeTravel
from market_data_service.domain.candles import Candle
from market_data_service.domain.instruments import INSTRUMENTS
from market_data_service.domain.market import Trade
from tests.fakes import FakeEventStore, InMemoryMarketStore


def at(clock: str) -> int:
    return int(datetime.fromisoformat(f"2026-11-20T{clock}+00:00").timestamp() * 1000)


def trade(n: int, clock: str, price: int, quantity: int = 10) -> Trade:
    return Trade(f"t{n}", n, price, quantity, "BUY", at(clock))


def market_with_history() -> InMemoryMarketStore:
    """Trades at 10:41:10, 10:42:05, 10:42:20, 10:42:40, and the 1m candles the live side stored."""
    store = InMemoryMarketStore()
    store.trade_rows["CALVERA"] = [
        trade(1, "10:41:10", 50_000),
        trade(2, "10:42:05", 51_000),
        trade(3, "10:42:20", 49_000),
        trade(4, "10:42:40", 52_000),
    ]
    for c in (
        Candle(at("10:40:00"), 49_500, 49_500, 49_500, 49_500, 0, 0),  # flat, from service start
        Candle(at("10:41:00"), 50_000, 50_000, 50_000, 50_000, 10, 1),
        Candle(at("10:42:00"), 51_000, 52_000, 49_000, 52_000, 30, 3),  # the whole minute
        Candle(at("10:43:00"), 52_000, 52_000, 52_000, 52_000, 0, 0),
    ):
        store.candle_rows[("CALVERA", "1m", c.bucket_start_ms)] = c
    return store


def market_at(store: InMemoryMarketStore, clock: str) -> MarketAt:
    return asyncio.run(TimeTravel(FakeEventStore(), store).market_at("CALVERA", at(clock)))


def test_the_candle_holding_t_counts_only_the_trades_up_to_t() -> None:
    m = market_at(market_with_history(), "10:42:30")
    assert [c.bucket_start_ms for c in m.candles] == [
        at("10:40:00"),
        at("10:41:00"),
        at("10:42:00"),
    ]
    assert m.candles[-1] == Candle(at("10:42:00"), 51_000, 51_000, 49_000, 49_000, 20, 2)
    assert [t.trade_id for t in m.recent_trades] == ["t3", "t2", "t1"]
    assert m.last_price_paise == 49_000


def test_a_minute_with_no_trade_yet_at_t_is_flat_at_the_previous_close() -> None:
    m = market_at(market_with_history(), "10:42:02")
    assert m.candles[-1] == Candle(at("10:42:00"), 50_000, 50_000, 50_000, 50_000, 0, 0)
    assert m.last_price_paise == 50_000


def test_before_any_trade_the_price_is_the_reference_price() -> None:
    m = market_at(market_with_history(), "10:40:30")
    reference = INSTRUMENTS["CALVERA"].reference_price_paise
    assert m.recent_trades == []
    assert m.last_price_paise == reference
    assert m.candles == [Candle(at("10:40:00"), reference, reference, reference, reference, 0, 0)]


def test_a_moment_when_market_data_was_not_running_shows_only_the_candles_before_it() -> None:
    store = market_with_history()
    del store.candle_rows[("CALVERA", "1m", at("10:43:00"))]
    m = market_at(store, "10:43:30")
    assert m.candles[-1].bucket_start_ms == at("10:42:00")
    assert m.candles[-1].close_paise == 52_000  # a finished minute is shown whole


def test_at_most_60_one_minute_candles() -> None:
    store = InMemoryMarketStore()
    for minute in range(90):
        start = at("09:00:00") + minute * 60_000
        store.candle_rows[("CALVERA", "1m", start)] = Candle(start, 500, 500, 500, 500, 0, 0)
    m = market_at(store, "10:29:59")
    assert len(m.candles) == 60
    assert m.candles[-1].bucket_start_ms == at("10:29:00")


def test_time_travel_reads_but_never_writes_the_read_models() -> None:
    store = market_with_history()
    market_at(store, "10:42:30")
    assert store.saves == 0


def test_an_empty_history_rebuilds_the_empty_book_at_sequence_0() -> None:
    m = market_at(InMemoryMarketStore(), "10:42:30")
    assert (m.book.sequence, m.book.depth(10)) == (0, ([], []))
    assert m.candles == [] and m.recent_trades == []
    assert datetime.fromtimestamp(m.at_ms / 1000, UTC).minute == 42
