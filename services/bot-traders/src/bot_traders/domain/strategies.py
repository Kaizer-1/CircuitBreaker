"""The four bot strategies (docs/01-seeds/01-actors.md: 4 market makers, 6 momentum, 5 mean
reversion, 5 noise), as pure decisions: given what a bot knows about one ticker, which orders to
place or cancel. No I/O; the randomness comes from the bot's own seeded generator.

**The interface is the extension point.** A strategy is anything with a `name` and
`decide(view, rng) -> list[Intent]`. A later behaviour, such as the Phase 7 panic sell for the
traffic-spike scenario (bots dump shares at market), is one more class here and a different
assignment in `roster()`; nothing else changes.

Why the market stays realistic:

- **Market makers** always quote both sides around a fair price, each at a different distance
  (8 to 20 basis points), so the spread stays tight. The fair price leans 2% towards the
  reference price every candle and away from inventory they hold too much of, so prices drift
  but do not run away.
- **Momentum** buys after a rise and sells after a fall (small MARKET orders): short trends.
- **Mean reversion** sells above and buys below its moving average (LIMIT orders): pulls back.
- **Noise** places small random orders: keeps trades flowing when nothing else does.
"""

from collections import deque
from dataclasses import dataclass, field
from random import Random
from typing import Protocol

from bot_traders.domain.market import (
    REFERENCE_PRICES,
    TICK_SIZE_PAISE,
    Cancel,
    Intent,
    OpenOrder,
    Place,
    ceil_tick,
    floor_tick,
)

STARTING_SHARES = 1_000  # what portfolio-service seeds every bot with, per ticker


@dataclass
class TickerView:
    """What one bot knows about one ticker when it decides."""

    ticker: str
    closes: deque[int]  # 5s closes, oldest first
    best_bid: int | None
    best_ask: int | None
    candle: int  # how many 5s candles of this ticker the bot has seen
    open_orders: list[OpenOrder] = field(default_factory=list)  # its own, on this ticker
    shares_held: int = 0
    activity: float = 1.0  # BOT_ACTIVITY: scales how often and how much

    @property
    def last(self) -> int:
        return self.closes[-1]

    @property
    def reference(self) -> int:
        return REFERENCE_PRICES[self.ticker]


class Strategy(Protocol):
    name: str

    def decide(self, view: TickerView, rng: Random) -> list[Intent]: ...


def size_for(notional_rupees: float, price: int, rng: Random, activity: float) -> int:
    """A quantity worth about `notional_rupees`, varied by ±40%."""
    quantity = notional_rupees * 100 * rng.uniform(0.6, 1.4) * max(activity, 0.1) / price
    return max(1, round(quantity))


def stale(view: TickerView, older_than: int) -> list[Intent]:
    return [
        Cancel(o.order_id, o.ticker)
        for o in view.open_orders
        if view.candle - o.placed_candle >= older_than
    ]


@dataclass
class MarketMaker:
    half_spread_bps: float
    notional_rupees: float = 6_000
    name: str = "market_maker"

    def decide(self, view: TickerView, rng: Random) -> list[Intent]:
        fair = view.last + 0.02 * (view.reference - view.last)
        # Holding more than it started with: quote lower to sell some (and the reverse).
        excess = (view.shares_held - STARTING_SHARES) / STARTING_SHARES
        skew = max(-30.0, min(30.0, excess * 40))
        bid = floor_tick(fair * (1 - (self.half_spread_bps + skew) / 10_000))
        ask = ceil_tick(fair * (1 + (self.half_spread_bps - skew) / 10_000))
        if ask - bid < 2 * TICK_SIZE_PAISE:
            ask = bid + 2 * TICK_SIZE_PAISE
        wanted = {"BUY": bid, "SELL": ask}
        intents: list[Intent] = []
        for side, price in wanted.items():
            mine = [o for o in view.open_orders if o.side == side]
            keep = [
                o
                for o in mine
                if o.limit_price_paise == price and view.candle - o.placed_candle < 6
            ]
            intents += [Cancel(o.order_id, o.ticker) for o in mine if o not in keep]
            if not keep:
                size = size_for(self.notional_rupees, price, rng, view.activity)
                intents.append(Place(view.ticker, side, size, price))
        return intents


@dataclass
class Momentum:
    lookback: int = 6
    threshold_bps: float = 8
    chance: float = 0.5
    notional_rupees: float = 4_000
    name: str = "momentum"

    def decide(self, view: TickerView, rng: Random) -> list[Intent]:
        if len(view.closes) <= self.lookback or rng.random() > self.chance * view.activity:
            return []
        move = (view.last - view.closes[-1 - self.lookback]) / view.closes[-1 - self.lookback]
        if abs(move) * 10_000 < self.threshold_bps:
            return []
        side = "BUY" if move > 0 else "SELL"
        return [
            Place(
                view.ticker,
                side,
                size_for(self.notional_rupees, view.last, rng, view.activity),
                None,
            )
        ]


@dataclass
class MeanReversion:
    window: int = 24
    threshold_bps: float = 20
    chance: float = 0.4
    notional_rupees: float = 5_000
    name: str = "mean_reversion"

    def decide(self, view: TickerView, rng: Random) -> list[Intent]:
        intents = stale(view, older_than=6)
        if len(view.closes) < 4 or rng.random() > self.chance * view.activity:
            return intents
        recent = list(view.closes)[-self.window :]
        average = 0.8 * sum(recent) / len(recent) + 0.2 * view.reference
        gap = (view.last - average) / average
        if abs(gap) * 10_000 < self.threshold_bps:
            return intents
        side = "SELL" if gap > 0 else "BUY"
        # Waits at the touch for the price to come back, a tick inside the spread if it can.
        if side == "SELL":
            price = (view.best_ask - TICK_SIZE_PAISE) if view.best_ask else ceil_tick(view.last)
            price = max(price, ceil_tick(view.last))
        else:
            price = (view.best_bid + TICK_SIZE_PAISE) if view.best_bid else floor_tick(view.last)
            price = min(price, floor_tick(view.last))
        size = size_for(self.notional_rupees, view.last, rng, view.activity)
        return [*intents, Place(view.ticker, side, size, price)]


@dataclass
class Noise:
    chance: float = 0.6
    market_share: float = 0.5  # of its orders, this many are MARKET orders
    notional_rupees: float = 2_000
    name: str = "noise"

    def decide(self, view: TickerView, rng: Random) -> list[Intent]:
        intents = stale(view, older_than=4)
        if rng.random() > self.chance * view.activity:
            return intents
        side = rng.choice(("BUY", "SELL"))
        size = size_for(self.notional_rupees, view.last, rng, view.activity)
        if rng.random() < self.market_share:
            return [*intents, Place(view.ticker, side, size, None)]
        offset = rng.uniform(0, 0.003) * view.last
        price = floor_tick(view.last - offset) if side == "BUY" else ceil_tick(view.last + offset)
        return [*intents, Place(view.ticker, side, size, price)]


@dataclass(frozen=True)
class BotPlan:
    name: str  # the seeded account, bot-01 ... bot-20
    strategy: Strategy
    tickers: tuple[str, ...]


def roster() -> list[BotPlan]:
    """The 20 bots: who runs which strategy on which tickers (a fixed plan, so runs repeat)."""
    tickers = ("CHAIWALA", "JUGAAD", "DABBA", "MONSOON", "FILMY")
    plans: list[BotPlan] = []
    for n, half_spread in enumerate((8, 12, 16, 20), start=1):
        plans.append(BotPlan(f"bot-{n:02d}", MarketMaker(half_spread), tickers))
    for i, n in enumerate(range(5, 11)):  # 6 momentum, 3 tickers each, rotating
        plans.append(BotPlan(f"bot-{n:02d}", Momentum(), _rotate(tickers, i, 3)))
    for i, n in enumerate(range(11, 16)):  # 5 mean reversion
        plans.append(BotPlan(f"bot-{n:02d}", MeanReversion(), _rotate(tickers, i, 3)))
    for i, n in enumerate(range(16, 21)):  # 5 noise
        plans.append(BotPlan(f"bot-{n:02d}", Noise(), _rotate(tickers, i, 2)))
    return plans


def _rotate(tickers: tuple[str, ...], start: int, count: int) -> tuple[str, ...]:
    return tuple(tickers[(start + k) % len(tickers)] for k in range(count))
