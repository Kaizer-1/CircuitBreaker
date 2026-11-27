"""After a panic sell the market comes back (owner, 2026-11-27): within 30 s of the panic's end,
every ticker has a bid and an ask again, so a phone can sell.

On staging the market stayed one-sided for minutes after the panic: no bids on any ticker. The
panic sells about 99 single shares a second into the market makers' bids, and the four makers'
cash (₹5,00,000 each) ran out in under a minute; with no cash they could post no bid, and nobody
bought their shares back. Here the 20 bots and the real panic run on the simulated exchange (real
reservation and matching rules) for as long as the agent's fix took on staging.
"""

import asyncio
from datetime import timedelta
from random import Random

import pytest

from bot_traders.application.panic import PanicSell
from bot_traders.domain.market import REFERENCE_PRICES
from bot_traders.domain.strategies import roster
from tests.unit.test_simulated_run import INTERVAL, T0, Run

WARM_UP_CANDLES = 60  # five minutes of normal trading first
PANIC_RATE, PANIC_SECONDS = 110, 150  # the calibrated rate, about as long as on staging
AFTER_CANDLES = 12  # 60 s: back within 30 s, and it stays
BACK_WITHIN = 6  # candles: 30 s
STARTING_CASH = 500_000_00  # paise, what portfolio-service seeds every bot with
MAKERS = {p.name for p in roster() if p.strategy.name == "market_maker"}


class PanicRun(Run):
    def __init__(self) -> None:
        super().__init__(seed=7)
        self.panic = PanicSell(
            self.exchange,
            rng=Random(11),  # noqa: S311 (a seeded simulation, not secrets)
            clock=self.time.clock,
            sleep=self.time.sleep,
        )
        self.two_sided_after: list[bool] = []  # each candle after the panic: all tickers?
        self.makers_cash_at_end = 0

    async def play(self) -> None:
        await self.fleet.start()
        panic_candles = PANIC_SECONDS // int(INTERVAL)
        total = WARM_UP_CANDLES + panic_candles + AFTER_CANDLES
        for n in range(1, total + 1):
            if n == WARM_UP_CANDLES + 1:
                start = T0 + timedelta(seconds=self.time.now)
                await self.panic.start(PANIC_RATE, PANIC_SECONDS, start)
            end = n * INTERVAL
            await self.time.run_until(end)
            for ticker in REFERENCE_PRICES:
                window = [
                    t for t in self.exchange.trades if t[0] == ticker and t[3] >= end - INTERVAL
                ]
                if window:
                    self.closes[ticker] = window[-1][1]
                await self.fleet.on_candle(self.candle(ticker, end))
            if n == WARM_UP_CANDLES + panic_candles:
                self.makers_cash_at_end = sum(
                    a.cash - a.reserved_cash
                    for name, a in self.exchange.accounts.items()
                    if name in MAKERS
                )
            if n > WARM_UP_CANDLES + panic_candles:
                self.two_sided_after.append(
                    all(all(self.exchange.best(t)) for t in REFERENCE_PRICES)
                )
        await self.panic.stop()


@pytest.fixture(scope="module")
def panic() -> PanicRun:
    run = PanicRun()
    asyncio.run(run.play())
    return run


def test_within_30_s_of_a_panic_every_ticker_has_a_bid_and_an_ask_and_keeps_them(
    panic: PanicRun,
) -> None:
    sold = sum(1 for _, _, order in panic.exchange.placed if order.limit_price_paise is None)
    assert sold > 5_000  # the panic really sold into the market
    late = panic.two_sided_after[BACK_WITHIN - 1 :]
    assert all(late), (
        f"from 30 s after the panic, a ticker had no bid or no ask at candle(s)"
        f" {[BACK_WITHIN + i for i, ok in enumerate(late) if not ok]} after it"
    )
    assert not panic.exchange.refusals


def test_market_makers_keep_most_of_their_cash_through_a_panic(panic: PanicRun) -> None:
    """The root cause on staging: they spent it all buying the panic's shares (1% left), then
    could post no bid. They pull a bid that one-sided selling keeps eating and come back with
    1-share probes, so a panic costs them almost nothing (about 105% left: they also sold in it,
    as bot accounts), however many panics a day brings."""
    start = STARTING_CASH * len(MAKERS)
    assert panic.makers_cash_at_end > 0.9 * start, (
        f"the market makers have ₹{panic.makers_cash_at_end / 100:,.0f} of"
        f" ₹{start / 100:,.0f} left after the panic"
    )
