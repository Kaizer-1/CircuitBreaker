"""The 20 bots over a long simulated run against an exchange with the real rules (tests/sim.py).

- They never place an order the exchange would refuse: tick size, quantity, funds (no margin),
  shares (no short selling). Not one refusal in an hour of trading.
- The market looks realistic: market makers keep the spread tight, prices drift but stay within
  ±10% of their reference (the band `scripts/bots_check.py` checks live), and trades keep coming.
- Orders flow through each interval instead of arriving in a burst at the candle boundary.
"""

from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any

import pytest

from bot_traders.application.fleet import Fleet
from bot_traders.domain.market import REFERENCE_PRICES, TICK_SIZE_PAISE
from bot_traders.runtime.events import build_event, deterministic_event_id
from tests.sim import SimExchange, VirtualTime

INTERVAL = 5.0
T0 = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)


class Run:
    def __init__(self, seed: int = 42, activity: float = 1.0) -> None:
        self.time = VirtualTime()
        self.exchange = SimExchange(self.time)
        self.results: list[tuple[str, str]] = []
        self.fleet = Fleet(
            self.exchange,
            seed=seed,
            activity=activity,
            interval_seconds=INTERVAL,
            wall_clock=lambda: T0 + timedelta(seconds=self.time.now),
            clock=self.time.clock,
            sleep=self.time.sleep,
            on_order=lambda strategy, result, ticker: self.results.append((strategy, result)),
        )
        self.spreads: dict[str, list[float]] = {t: [] for t in REFERENCE_PRICES}
        self.closes: dict[str, int] = dict(REFERENCE_PRICES)

    async def candles(self, count: int) -> None:
        await self.fleet.start()
        for n in range(1, count + 1):
            end = n * INTERVAL
            await self.time.run_until(end)  # the interval's orders go out
            for ticker in REFERENCE_PRICES:
                window = [
                    t for t in self.exchange.trades if t[0] == ticker and t[3] >= end - INTERVAL
                ]
                if window:
                    self.closes[ticker] = window[-1][1]
                bid, ask = self.exchange.best(ticker)
                if bid and ask:
                    self.spreads[ticker].append((ask - bid) / ((ask + bid) / 2))
                await self.fleet.on_candle(self.candle(ticker, end))
        await self.time.run_until((count + 1) * INTERVAL)
        await self.fleet.wait_idle()

    def candle(self, ticker: str, end_seconds: float) -> Any:
        start = T0 + timedelta(seconds=end_seconds - INTERVAL)
        bucket_start = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        bucket_end = (start + timedelta(seconds=INTERVAL)).strftime("%Y-%m-%dT%H:%M:%SZ")
        close = self.closes[ticker]
        return build_event(
            "CandleClosed",
            {
                "ticker": ticker,
                "interval": "5s",
                "bucket_start": bucket_start,
                "bucket_end": bucket_end,
                "open_paise": close,
                "high_paise": close,
                "low_paise": close,
                "close_paise": close,
                "volume": 0,
                "trade_count": 0,
            },
            producer="market-data-service",
            environment="dev",
            correlation_id="sim",
            event_id=deterministic_event_id(f"{ticker}|5s|{bucket_start}"),
        )


@pytest.fixture(scope="module")
def hour() -> Run:
    """One simulated hour (720 candles of 5 s) of all 20 bots, run once for the tests below."""
    import asyncio

    run = Run()
    asyncio.run(run.candles(720))
    return run


def test_not_one_order_is_refused_in_an_hour_of_trading(hour: Run) -> None:
    assert hour.exchange.refusals == []
    assert ("market_maker", "refused") not in hour.results
    assert len(hour.exchange.placed) > 10_000  # it really traded


def test_open_orders_are_listed_only_at_start_up(hour: Run) -> None:
    # Q-07 scans a trader's order history, so bots learn about fills from their Q-10 stream.
    assert hour.exchange.order_lists == 20


def test_every_order_has_a_valid_shape(hour: Run) -> None:
    for _, _, order in hour.exchange.placed:
        assert 1 <= order.quantity <= 10_000
        if order.limit_price_paise is not None:
            assert order.limit_price_paise >= TICK_SIZE_PAISE
            assert order.limit_price_paise % TICK_SIZE_PAISE == 0


def test_no_account_ever_goes_short_or_into_debt(hour: Run) -> None:
    for account in hour.exchange.accounts.values():
        assert account.cash >= account.reserved_cash >= 0
        assert all(q >= 0 for q in account.shares.values())


def test_market_makers_keep_the_spread_tight(hour: Run) -> None:
    for ticker, spreads in hour.spreads.items():
        assert len(spreads) > 600, ticker  # both sides quoted almost all the time
        assert median(spreads) < 0.004, (ticker, median(spreads))  # under 0.4%


def test_prices_drift_but_stay_near_their_reference(hour: Run) -> None:
    for ticker, reference in REFERENCE_PRICES.items():
        prices = [p for t, p, _, _ in hour.exchange.trades if t == ticker]
        assert len(prices) > 300, ticker  # trades keep coming
        assert len(set(prices)) > 10, ticker  # and the price moves
        assert all(0.90 * reference <= p <= 1.10 * reference for p in prices), ticker


def test_every_strategy_trades(hour: Run) -> None:
    placed = {strategy for strategy, result in hour.results if result == "placed"}
    assert placed == {"market_maker", "momentum", "mean_reversion", "noise"}


def test_orders_are_spread_through_the_interval_not_bunched_at_the_candle(hour: Run) -> None:
    # When each order went out, as a fraction of the 5 s interval after the candle that caused
    # it (candles close at multiples of 5 s in the simulation).
    phases = [(t % INTERVAL) / INTERVAL for _, t, _ in hour.exchange.placed if t > INTERVAL]
    bins = [0] * 10
    for phase in phases:
        bins[min(int(phase * 10), 9)] += 1
    shares = [b / len(phases) for b in bins]
    assert shares[0] < 0.07, shares  # not a burst right after the candle closes
    assert all(0.06 < s < 0.16 for s in shares[1:9]), shares  # roughly even through the interval


def test_the_same_seed_repeats_the_same_orders() -> None:
    import asyncio

    first, second = Run(seed=7), Run(seed=7)
    asyncio.run(first.candles(30))
    asyncio.run(second.candles(30))
    assert [o for _, _, o in first.exchange.placed] == [o for _, _, o in second.exchange.placed]
    other = Run(seed=8)
    asyncio.run(other.candles(30))
    assert [o for _, _, o in other.exchange.placed] != [o for _, _, o in first.exchange.placed]


def test_activity_scales_the_order_rate() -> None:
    import asyncio

    calm, busy = Run(activity=0.5), Run(activity=2.0)
    asyncio.run(calm.candles(60))
    asyncio.run(busy.candles(60))
    assert len(busy.exchange.placed) > 1.3 * len(calm.exchange.placed)
    assert busy.exchange.refusals == calm.exchange.refusals == []
