"""The four bot strategies (docs/01-seeds/01-actors.md: 4 market makers, 6 momentum, 5 mean
reversion, 5 noise), as pure decisions: given what a bot knows about one ticker, which orders to
place or cancel. No I/O; the randomness comes from the bot's own seeded generator.

**The interface is the extension point.** A strategy is anything with a `name` and
`decide(view, rng) -> list[Intent]`. A later behaviour, such as the Phase 7 panic sell for the
traffic-spike scenario (bots dump shares at market), is one more class here and a different
assignment in `roster()`; nothing else changes.

Why the market stays realistic:

- **Market makers** always quote both sides around a fair price, each at a different distance
  (8 to 20 basis points), so the spread stays tight. The fair price leans 5% towards the
  reference price every candle and away from inventory they hold too much of (at most 15 basis
  points), so prices drift but do not run away.
- **Momentum** buys after a rise and sells after a fall (small MARKET orders): short trends.
  A move must be 8 basis points and at least 3 ticks.

Phase 4 rebalance: with a 2% lean, an inventory lean of up to 30 basis points and an 8 basis
point momentum trigger, HELIOR (₹88.20, where one tick is 5.7 basis points) ran 13 to 15% above
its reference within an hour in 3 of 5 simulated runs. A market maker that had sold shares bid
above its fair price to buy them back, the last price followed, and momentum chased a two-tick
wiggle as a trend. Now the worst of 12 simulated hours is 5.3% (most stay within 2.6%), and
`scripts/bots_check.py` holds every ticker within ±10% of its reference.
- **Market makers pull a bid that one-sided selling keeps eating** (2026-11-27): if two of a maker's
  bids on a ticker are filled away within 3 s of being placed, within 10 s, it posts no bid there
  for 15 s (it keeps its ask), then bids 1 share at a time for 15 s (a probe); a probe eaten at
  once means the selling goes on, so it backs off again; then full bids. In a
  panic sell every new bid is hit as soon as it appears: in the simulation the four makers spent
  ₹19,82,000 of their ₹20,00,000 buying the panic's shares (staging showed the same: bids skipped
  for lack of cash), and with no cash they could post no bid at all, so no phone could sell after
  the panic. Over many panics staging's makers were left with 3–26% of their cash. Now a panic
  costs them almost nothing (simulated: about 105% left, as they also sold in it) and every
  ticker has a bid and an ask again within 15–20 s of the panic's end (owner's limit: 30 s). A
  bid eaten before the answer to its placement came back counts too: under load the fill
  overtakes the answer, and on staging the guard first never tripped. In a normal simulated hour
  the makers average 3.88 bids per ticker instead of 3.99 (asks unchanged).
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
    Requote,
    ceil_tick,
    floor_tick,
)

STARTING_SHARES = 1_000  # what portfolio-service seeds every bot with, per ticker


@dataclass
class OneSidedFlow:
    """One maker's guard on one ticker: is selling eating its bids as fast as it posts them?
    Only a bid eaten within `quick_seconds` of being placed counts: in a panic every new bid is
    gone within about a second, while a normal bid rests for tens of seconds before a fill.
    `at` and `lived` are the bot's clock in seconds (pure: the caller says what time it is)."""

    quick_seconds: float = 3.0
    window_seconds: float = 10.0
    # Bids eaten quickly within the window. 4, not 2 (CI, 2026-11-28): in a busy normal market a
    # maker's bid is eaten quickly 3 times in 10 s at most (simulated hours, four seeds), so 2
    # tripped about 100 times an hour and thinned the bids; a panic reaches 5 to 14.
    trips_after: int = 4
    cool_off_seconds: float = 15.0
    probe_seconds: float = 15.0  # after the cool-off, 1-share bids for this long
    trips: int = 0  # how often the guard has stepped back (the simulation counts them)
    _emptied: deque[float] = field(default_factory=deque)
    _pulled_until: float = float("-inf")
    _probe_until: float = float("-inf")

    def bid_filled_away(self, at: float, lived: float) -> None:
        if lived > self.quick_seconds:
            return  # an ordinary fill
        self._emptied.append(at)
        while self._emptied and at - self._emptied[0] > self.window_seconds:
            self._emptied.popleft()
        if len(self._emptied) >= self.trips_after or at < self._probe_until:
            # (a probe eaten at once: the selling goes on, so back off again)
            self._pulled_until = at + self.cool_off_seconds
            self._probe_until = self._pulled_until + self.probe_seconds
            self._emptied.clear()
            self.trips += 1

    def bid_pulled(self, at: float) -> bool:
        return at < self._pulled_until

    def bid_probing(self, at: float) -> bool:
        """After the cool-off: bid 1 share only, until a while passes with no bid eaten at once."""
        return self._pulled_until <= at < self._probe_until


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
    bid_pulled: bool = False  # a market maker's OneSidedFlow guard says: no bid for now
    bid_probing: bool = False  # ... or: a 1-share bid only, to see whether the selling is over

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
    reference_lean: float = 0.05  # of the gap to the reference price, every candle
    max_inventory_skew_bps: float = 15
    name: str = "market_maker"

    def decide(self, view: TickerView, rng: Random) -> list[Intent]:
        fair = view.last + self.reference_lean * (view.reference - view.last)
        # Holding more than it started with: quote lower to sell some (and the reverse), but
        # never so far that it chases the price to get its shares back.
        excess = (view.shares_held - STARTING_SHARES) / STARTING_SHARES
        cap = self.max_inventory_skew_bps
        skew = max(-cap, min(cap, excess * 40))
        bid = floor_tick(fair * (1 - (self.half_spread_bps + skew) / 10_000))
        ask = ceil_tick(fair * (1 + (self.half_spread_bps - skew) / 10_000))
        if ask - bid < 2 * TICK_SIZE_PAISE:
            ask = bid + 2 * TICK_SIZE_PAISE
        wanted = {"SELL": ask} if view.bid_pulled else {"BUY": bid, "SELL": ask}
        intents: list[Intent] = [
            Cancel(o.order_id, o.ticker)
            for o in view.open_orders
            if o.side == "BUY" and view.bid_pulled
        ]
        for side, price in wanted.items():
            mine = [o for o in view.open_orders if o.side == side]
            keep = [
                o
                for o in mine
                if o.limit_price_paise == price
                and view.candle - o.placed_candle < 6
                and (side == "SELL" or (o.remaining <= 1) == view.bid_probing)
            ]
            replaced = tuple(Cancel(o.order_id, o.ticker) for o in mine if o not in keep)
            if keep:
                intents += replaced  # a good quote stays; anything else on this side goes
            else:
                size = size_for(self.notional_rupees, price, rng, view.activity)
                if side == "BUY" and view.bid_probing:
                    size = 1
                intents.append(Requote(Place(view.ticker, side, size, price), replaced))
        return intents


@dataclass
class Momentum:
    lookback: int = 6
    threshold_bps: float = 8
    min_ticks: int = 3  # on a cheap ticker 8 basis points is under 2 ticks: noise, not a trend
    chance: float = 0.5
    notional_rupees: float = 4_000
    name: str = "momentum"

    def decide(self, view: TickerView, rng: Random) -> list[Intent]:
        if len(view.closes) <= self.lookback or rng.random() > self.chance * view.activity:
            return []
        before = view.closes[-1 - self.lookback]
        move = view.last - before
        if abs(move) < max(self.threshold_bps * before / 10_000, self.min_ticks * TICK_SIZE_PAISE):
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
    tickers = ("CALVERA", "KORVIN", "ASTRYX", "HELIOR", "TAVIRA")
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
