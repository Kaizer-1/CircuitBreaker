"""The 20 bots in one process (services/bot-traders/README.md: one service, one replica).

The fleet logs every bot in (A-03) and opens its portfolio stream (Q-10), keeps one shared view of
each ticker (its 5s closes from `CandleClosed`, and its best bid and ask from Q-04, read once per
candle for all bots), and hands each `CandleClosed` to the bots that trade that ticker. Rules:

- Only 5s candles drive decisions (the heartbeat); 1m and 5m are ignored.
- A repeated `CandleClosed` (same deterministic `event_id`, e.g. from a second market-data
  replica, or a corrected candle) is ignored, and so is a stale one (it closed more than two
  intervals ago: the consumer is catching up).
- Market makers quote once at start-up, so the very first trade has someone to trade with.
"""

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime
from random import Random
from typing import Any

from bot_traders.application.bot import (
    REQUOTE_WINDOW_SECONDS,
    Bot,
    MarketState,
    OrderListener,
    TimingListener,
)
from bot_traders.domain.market import REFERENCE_PRICES, TICKERS
from bot_traders.domain.strategies import BotPlan, MarketMaker, roster
from bot_traders.ports.exchange import Exchange, SessionExpiredError
from bot_traders.runtime.events import EventEnvelope, parse_timestamp

logger = logging.getLogger("fleet")

HISTORY = 120  # 5s closes kept per ticker (10 minutes)
SEEN_EVENTS = 5_000
LISTINGS_AT_ONCE = 2  # bots listing their open orders (Q-07) at the same moment


def bot_rng(seed: int | None, name: str) -> Random:
    """Each bot's own generator: repeatable with BOT_RANDOM_SEED, different without it."""
    # A seeded simulation, not security: Random is the right tool.
    return Random(f"{seed}:{name}") if seed is not None else Random()  # noqa: S311


class Fleet:
    def __init__(
        self,
        exchange: Exchange,
        *,
        seed: int | None = None,
        activity: float = 1.0,
        interval_seconds: float = 5.0,
        plans: list[BotPlan] | None = None,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_order: OrderListener = lambda strategy, result, ticker: None,
        on_timing: TimingListener = lambda kind, strategy, seconds: None,
    ) -> None:
        self._exchange = exchange
        self._interval = interval_seconds
        self._wall_clock = wall_clock
        # Listing open orders (Q-07) reads a bot's whole order history; after a gateway restart all
        # 20 bots list at once. A few at a time keeps order-service answering everyone else.
        listing_slots = asyncio.Semaphore(LISTINGS_AT_ONCE)
        self.bots = [
            Bot(
                plan,
                exchange,
                bot_rng(seed, plan.name),
                activity=activity,
                interval_seconds=interval_seconds,
                clock=clock,
                sleep=sleep,
                on_order=on_order,
                on_timing=on_timing,
                requote=self._requote_soon,
                listing_slots=listing_slots,
            )
            for plan in (plans if plans is not None else roster())
        ]
        self.market = {t: MarketState([REFERENCE_PRICES[t]]) for t in TICKERS}
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._tasks: set[asyncio.Task[None]] = set()
        self._listeners: set[asyncio.Task[None]] = set()
        self.started = False

    async def start(self) -> None:
        """Log every bot in, take the last prices (Q-02), and let the market makers quote."""
        for bot in self.bots:
            await bot.login()
            listener = asyncio.create_task(bot.listen())  # Q-10: fills and closed orders
            self._listeners.add(listener)
        session = self.bots[0].signed_in()
        for instrument in await self._exchange.instruments(session):
            if instrument["ticker"] in self.market:
                self.market[instrument["ticker"]].closes = [int(instrument["last_price_paise"])]
        for ticker in TICKERS:
            await self._read_depth(ticker)
        self.started = True
        for bot in self.bots:
            if isinstance(bot.plan_.strategy, MarketMaker):
                for ticker in bot.plan_.tickers:
                    self._spawn(bot.act(ticker, self.market[ticker]))
        logger.info("bots signed in and quoting", extra={"bots": len(self.bots)})

    async def on_candle(self, envelope: EventEnvelope) -> bool:
        """A CandleClosed from candles.v1. True if the bots acted on it."""
        d = envelope.data
        if envelope.event_type != "CandleClosed" or d.get("interval") != "5s" or not self.started:
            return False
        if envelope.event_id in self._seen:
            return False
        self._seen[envelope.event_id] = None
        if len(self._seen) > SEEN_EVENTS:
            self._seen.popitem(last=False)
        age = (self._wall_clock() - parse_timestamp(d["bucket_end"])).total_seconds()
        ticker = d["ticker"]
        if age > 2 * self._interval or ticker not in self.market:
            return False  # catching up on old candles: nothing to act on
        state = self.market[ticker]
        state.closes = [*state.closes, int(d["close_paise"])][-HISTORY:]
        state.candle += 1
        await self._read_depth(ticker)
        for bot in self.bots:
            if ticker in bot.plan_.tickers:
                self._spawn(bot.act(ticker, state))
        return True

    def _requote_soon(self, bot: Bot, ticker: str) -> None:
        """A market maker's side was filled: a quick step on that ticker, not the next candle."""
        if self.started:
            self._spawn(bot.act(ticker, self.market[ticker], window=REQUOTE_WINDOW_SECONDS))

    async def _read_depth(self, ticker: str) -> None:
        reader = self.bots[0]
        try:
            await reader.ensure_session()
            book = await self._exchange.depth(reader.signed_in(), ticker)
        except SessionExpiredError:
            reader.session = None  # signs in again on the next read
            return
        except Exception as exc:
            logger.warning("depth not read", extra={"ticker": ticker, "error": repr(exc)})
            return
        state = self.market[ticker]
        state.best_bid, state.best_ask = book.get("best_bid_paise"), book.get("best_ask_paise")

    def _spawn(self, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @property
    def idle(self) -> bool:
        """No bot step is running (for tests driving a virtual clock)."""
        return not self._tasks

    async def wait_idle(self) -> None:
        """For tests: until every bot has sent this interval's orders."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks))

    def stop(self) -> None:
        for task in self._tasks | self._listeners:
            task.cancel()
