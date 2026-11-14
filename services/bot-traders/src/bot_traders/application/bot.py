"""One bot: refresh what it owns, let its strategy decide, then send the orders spread over the
next candle interval.

**Knowing which orders still rest.** The bot follows its own Q-10 portfolio stream: `fill` and
`order_closed` tell it at once when an order traded or finished. It lists its open orders (Q-07)
only at start-up and after the stream reconnects, because that query scans the trader's order
history; polling it from 20 bots slowed order placement for everyone.

**Timing (an implementation detail, services/bot-traders/README.md).** Bots act on
`CandleClosed` (policy P-09), but if every bot sent its orders the moment a candle closed, trades
would arrive in a burst every 5 s. So each bot spreads its orders over the following interval at
random offsets (5% to 95% of it) from its own generator, seeded by `BOT_RANDOM_SEED`.

**A market maker re-quotes a filled side at once** (2026-11-14). Waiting for the next candle left
the side without its quote for 3.3 s on average, up to about 8 s, after every fill: on a busy
ticker the four makers then averaged fewer than 3.5 quotes on a side (a CI failure). Now a fill that
empties a maker's side asks for a quick step on that ticker, spread over REQUOTE_WINDOW_SECONDS.
"""

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from random import Random
from typing import Any

from bot_traders.domain.ledger import Ledger
from bot_traders.domain.market import Cancel, Intent, OpenOrder, Place, Requote
from bot_traders.domain.strategies import BotPlan, MarketMaker, TickerView
from bot_traders.ports.exchange import Exchange, OrderRefusedError, Session, SessionExpiredError

logger = logging.getLogger("bot")

REFRESH_AFTER_SECONDS = 4.0  # a portfolio read is reused for this long
# After a failed step the bot pauses, doubling each time (1 s, 2 s, 4 s ... 30 s), like the
# stream's reconnect. Without it, 20 bots retrying a slow listing every candle kept order-service
# saturated after a gateway restart (Phase 4: the abandoned listings piled up until it ran out of
# memory). A step that works resets the pause.
STEP_BACKOFF_MAX_SECONDS = 30.0
CLOSED_REMEMBERED = 2_000  # finished order IDs kept, for a close that overtakes its A-05 answer
SPREAD = (0.05, 0.95)  # orders go out between 5% and 95% of the interval
REQUOTE_WINDOW_SECONDS = 1.0  # a market maker's quick step after a fill spreads over this


def spread(count: int, interval_seconds: float, rng: Random) -> list[float]:
    """Offsets for `count` orders inside one interval, in increasing order."""
    low, high = SPREAD
    return sorted(rng.uniform(low, high) * interval_seconds for _ in range(count))


@dataclass
class MarketState:
    """What the fleet knows about one ticker, shared by its bots."""

    closes: list[int]
    best_bid: int | None = None
    best_ask: int | None = None
    candle: int = 0


OrderListener = Callable[[str, str, str], None]  # strategy, result, ticker
# kind, strategy, seconds. "lateness": how much later than planned an order went out (a starved
# event loop or slow answers to the orders before it). "quote_gap": how long a side had no resting
# order of this bot after the exchange closed its last one (filled), until a new one was placed.
TimingListener = Callable[[str, str, float], None]
Requoter = Callable[["Bot", str], None]  # asks the fleet for a quick step of this bot on a ticker


class Bot:
    def __init__(
        self,
        plan: BotPlan,
        exchange: Exchange,
        rng: Random,
        *,
        activity: float = 1.0,
        interval_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_order: OrderListener = lambda strategy, result, ticker: None,
        on_timing: TimingListener = lambda kind, strategy, seconds: None,
        requote: Requoter = lambda bot, ticker: None,
        listing_slots: asyncio.Semaphore | None = None,
    ) -> None:
        self.plan_ = plan
        self.name = plan.name
        self._exchange = exchange
        self.rng = rng
        self._activity = activity
        self._interval = interval_seconds
        self._clock = clock
        self._sleep = sleep
        self._on_order = on_order
        self._on_timing = on_timing
        self._bare: dict[tuple[str, str], float] = {}  # (ticker, side): no resting order since
        self._requote = requote
        self._again: set[str] = set()  # tickers to re-quote once the current step on them ends
        self.session: Session | None = None
        self.ledger = Ledger()
        self.open_orders: dict[str, OpenOrder] = {}
        self._refreshed_at: float | None = None
        self._reconcile = True  # list open orders (Q-07) on the next refresh
        self._refreshing = asyncio.Lock()
        self._closed: dict[str, None] = {}  # recently finished order IDs, oldest first
        self.busy: set[str] = set()  # tickers this bot is still acting on
        self._signing_in = asyncio.Lock()
        self._listing_slots = listing_slots or asyncio.Semaphore(1)
        self._backoff = 0.0  # seconds; 0 while steps work
        self._resume_at = 0.0  # no step before this time after a failure

    @property
    def strategy_name(self) -> str:
        return self.plan_.strategy.name

    async def login(self) -> None:
        self.session = await self._exchange.login(self.name)

    async def ensure_session(self) -> None:
        """Sign in again after the token was refused (sessions last 12 hours, A-03)."""
        async with self._signing_in:
            if self.session is None:
                await self.login()

    def _expired(self) -> None:
        if self.session is not None:
            logger.info("session expired: signing in again", extra={"bot": self.name})
        self.session = None
        self._reconcile = True  # what finished meanwhile is unknown

    def signed_in(self) -> Session:
        if self.session is None:
            raise RuntimeError(f"{self.name} has not signed in")
        return self.session

    async def refresh(self, *, force: bool = False) -> None:
        # A market maker acts on five tickers at once: they share one refresh, not five.
        async with self._refreshing:
            now = self._clock()
            fresh = (
                self._refreshed_at is not None and now - self._refreshed_at < REFRESH_AFTER_SECONDS
            )
            if fresh and not force:
                return
            portfolio = await self._exchange.portfolio(self.signed_in())
            self.ledger.refresh(portfolio, requested_at=now)
            self._refreshed_at = now
            if self._reconcile:
                await self._list_open_orders()

    async def _list_open_orders(self) -> None:
        async with self._listing_slots:  # shared by the fleet: a few bots at a time
            orders = await self._exchange.open_orders(self.signed_in())
        self._reconcile = False
        known = self.open_orders
        self.open_orders = {
            o["order_id"]: OpenOrder(
                o["order_id"],
                o["ticker"],
                o["side"],
                o.get("limit_price_paise"),
                int(o.get("remaining_quantity") or o["quantity"]),
                known[o["order_id"]].placed_candle if o["order_id"] in known else -1,
            )
            for o in orders
        }

    async def listen(self) -> None:
        """Follow the Q-10 stream for `fill` and `order_closed`; reconnect with back-off."""
        delay = 1.0
        while True:
            try:
                await self.ensure_session()
                async for message in self._exchange.portfolio_stream(self.signed_in()):
                    delay = 1.0
                    self.on_stream(message)
            except asyncio.CancelledError:
                raise
            except SessionExpiredError:
                self._expired()
            except Exception as exc:
                logger.warning(
                    "portfolio stream lost", extra={"bot": self.name, "error": repr(exc)}
                )
            self._reconcile = True  # something may have finished while we were away
            await self._sleep(delay)
            delay = min(delay * 2, 30.0)

    def on_stream(self, message: dict[str, Any]) -> None:
        data = message.get("data") or {}
        order_id = data.get("order_id")
        if message.get("type") == "fill" and order_id in self.open_orders:
            remaining = int(data["order_remaining_quantity"])
            if remaining == 0:
                self._forget(order_id)
            else:
                self.open_orders[order_id].remaining = remaining
        elif message.get("type") == "order_closed" and order_id:
            self._forget(order_id)

    def _forget(self, order_id: str) -> None:
        """The exchange closed this order (filled, or finished elsewhere)."""
        gone = self.open_orders.pop(order_id, None)
        if gone is not None and gone.limit_price_paise is not None:
            same_side = (gone.ticker, gone.side)
            if not any((o.ticker, o.side) == same_side for o in self.open_orders.values()):
                self._bare.setdefault(same_side, self._clock())
                if isinstance(self.plan_.strategy, MarketMaker):
                    self._requote(self, gone.ticker)  # a maker never waits a candle for a quote
        self._closed[order_id] = None
        if len(self._closed) > CLOSED_REMEMBERED:
            self._closed.pop(next(iter(self._closed)))

    def decide(
        self, ticker: str, market: MarketState, window: float | None = None
    ) -> list[tuple[float, Intent]]:
        """What to do on this ticker during the next interval (or `window` seconds), as (offset
        seconds, intent)."""
        view = TickerView(
            ticker=ticker,
            closes=deque(market.closes),
            best_bid=market.best_bid,
            best_ask=market.best_ask,
            candle=market.candle,
            open_orders=[
                o if o.placed_candle >= 0 else _aged(o, market.candle)
                for o in self.open_orders.values()
                if o.ticker == ticker
            ],
            shares_held=self.ledger.quantity.get(ticker, 0),
            activity=self._activity,
        )
        intents = self.plan_.strategy.decide(view, self.rng)
        span = window if window is not None else self._interval
        return list(zip(spread(len(intents), span, self.rng), intents, strict=True))

    async def act(self, ticker: str, market: MarketState, *, window: float | None = None) -> None:
        """Refresh, decide, and send this ticker's orders through the next interval, or through
        `window` seconds (a market maker's quick step after a fill)."""
        if ticker in self.busy:
            if window is not None:
                self._again.add(ticker)  # a fill during this step: re-quote when it ends
            return  # still busy with the previous candle: skip this one
        if self._clock() < self._resume_at:
            return  # backing off after a failure: give the exchange room to recover
        self.busy.add(ticker)
        try:
            await self.ensure_session()
            await self.refresh()
            elapsed = 0.0
            began = self._clock()
            for offset, intent in self.decide(ticker, market, window):
                await self._sleep(offset - elapsed)
                elapsed = offset
                self._on_timing("lateness", self.strategy_name, self._clock() - began - offset)
                await self.execute(intent, market)
            self._backoff = 0.0
        except SessionExpiredError:
            self._expired()  # the next step signs in first
        except Exception as exc:
            self._backoff = min(self._backoff * 2 or 1.0, STEP_BACKOFF_MAX_SECONDS)
            self._resume_at = self._clock() + self._backoff
            logger.warning(
                "bot step failed",
                extra={"bot": self.name, "error": repr(exc), "retry_in_seconds": self._backoff},
            )
        finally:
            self.busy.discard(ticker)
            if ticker in self._again:
                self._again.discard(ticker)
                self._requote(self, ticker)

    async def execute(self, intent: Intent, market: MarketState) -> None:
        if isinstance(intent, Requote):  # the new quote first: no moment without one
            await self._place(intent.place, market)
            for cancel in intent.cancels:
                await self.execute(cancel, market)
            return
        if isinstance(intent, Cancel):
            if intent.order_id in self.open_orders:
                await self._exchange.cancel(self.signed_in(), intent.order_id)
                self.open_orders.pop(intent.order_id, None)
                self._on_order(self.strategy_name, "cancelled", intent.ticker)
            return
        await self._place(intent, market)

    async def _place(self, order: Place, market: MarketState) -> None:
        last = market.closes[-1]
        if self.ledger.refusal(order, last) is not None:
            self._on_order(self.strategy_name, "skipped", order.ticker)  # kept back by the ledger
            return
        try:
            placed = await self._exchange.place(self.signed_in(), order)
        except OrderRefusedError as refused:
            logger.error(
                "the exchange refused a bot order",
                extra={"bot": self.name, "code": refused.code, "order": repr(order)},
            )
            self._on_order(self.strategy_name, "refused", order.ticker)
            return
        self.ledger.hold(order, last, answered_at=self._clock())
        # A MARKET order never rests; a LIMIT one may already have finished (its order_closed
        # can overtake this answer).
        bare_since = self._bare.pop((order.ticker, order.side), None)
        if order.limit_price_paise is not None and bare_since is not None:
            self._on_timing("quote_gap", self.strategy_name, self._clock() - bare_since)
        if order.limit_price_paise is not None and placed["order_id"] not in self._closed:
            self.open_orders[placed["order_id"]] = OpenOrder(
                placed["order_id"],
                order.ticker,
                order.side,
                order.limit_price_paise,
                order.quantity,
                market.candle,
            )
        self._on_order(self.strategy_name, "placed", order.ticker)


def _aged(order: OpenOrder, candle: int) -> OpenOrder:
    """An order found at start-up (placed before a restart): treat it as placed just now."""
    order.placed_candle = candle
    return order
