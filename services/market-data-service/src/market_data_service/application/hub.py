"""The live market over WebSockets (Q-06, api/README.md): a snapshot first, then updates.

A client gets one `snapshot` on connect (depth, last price, the last 60 5s candles and the last
50 trades, all as of one sequence), then:

- `trade` for every trade, at once (each has its `sequence`: anything at or below the snapshot's
  `order_book.as_of_sequence` is already in it);
- `book`, the full depth, at most 4 times a second (each has its `as_of_sequence`);
- `candle`: the forming candle of each interval it subscribed to, every second, and every closed
  candle once with `closed: true` (a candle replaces the one with the same `bucket_start`).

Registering a client and building its snapshot happen in one step with no `await` in between,
so no update can fall between the two. A client that falls 1,000 messages behind is closed
(1011) and reconnects for a fresh snapshot: dropping messages would leave it silently wrong.
"""

import asyncio
import logging
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from market_data_service.application.projection import MarketProjection
from market_data_service.application.views import candle_view, depth_view, trade_view
from market_data_service.domain.candles import INTERVALS, Candle
from market_data_service.domain.market import Trade

logger = logging.getLogger("hub")

BOOK_EVERY_SECONDS = 0.25  # at most 4 book messages per second
QUEUE_LIMIT = 1_000
BOOK_LEVELS = 10
SNAPSHOT_CANDLES = 60
SNAPSHOT_TRADES = 50
DEFAULT_INTERVALS = ("5s",)

Message = dict[str, Any]


class Subscriber:
    def __init__(self, ticker: str, intervals: Iterable[str]) -> None:
        self.ticker = ticker
        self.intervals = set(intervals)
        self.queue: asyncio.Queue[Message] = asyncio.Queue(QUEUE_LIMIT)
        self.lagging = False  # fell too far behind: the connection must be closed

    def send(self, message: Message) -> None:
        if self.lagging:
            return
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            self.lagging = True


class MarketHub:
    def __init__(self, projection: MarketProjection) -> None:
        self._projection = projection
        projection.listener = self
        self._subscribers: dict[str, set[Subscriber]] = defaultdict(set)
        self._dirty_books: set[str] = set()

    @property
    def ready(self) -> bool:
        return self._projection.ready

    def connections(self) -> int:
        return sum(len(s) for s in self._subscribers.values())

    def subscribe(
        self, ticker: str, intervals: Iterable[str] = DEFAULT_INTERVALS
    ) -> tuple[Message, Subscriber]:
        """Register a client and build its snapshot, atomically (no await)."""
        market = self._projection.markets[ticker]
        candles: list[Candle] = []
        if market.series is not None:
            five = market.series["5s"]
            last = five.forming_start_ms
            candles = five.candles(last - (SNAPSHOT_CANDLES - 1) * INTERVALS["5s"], last)
        snapshot = {
            "type": "snapshot",
            "data": {
                "order_book": depth_view(market, BOOK_LEVELS, self._projection.now_ms()),
                "last_price_paise": market.last_price_paise,
                "candles": [candle_view(c) for c in candles],
                "recent_trades": [trade_view(t) for t in list(market.recent)[:SNAPSHOT_TRADES]],
            },
        }
        subscriber = Subscriber(ticker, intervals)
        self._subscribers[ticker].add(subscriber)
        return snapshot, subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        self._subscribers[subscriber.ticker].discard(subscriber)

    # ------------------------------------------------------------------ MarketListener

    def on_trade(self, ticker: str, trade: Trade) -> None:
        message = {"type": "trade", "data": trade_view(trade)}
        for subscriber in self._subscribers[ticker]:
            subscriber.send(message)

    def on_book(self, ticker: str) -> None:
        self._dirty_books.add(ticker)  # sent by the next flush: at most 4 per second

    def on_candle(self, ticker: str, interval: str, candle: Candle, closed: bool) -> None:
        if closed:
            self._send_candle(ticker, interval, candle, closed=True)

    # ------------------------------------------------------------------ the timed messages

    def flush_books(self) -> None:
        now = self._projection.now_ms()
        for ticker in self._dirty_books:
            if self._subscribers[ticker]:
                market = self._projection.markets[ticker]
                message = {"type": "book", "data": depth_view(market, BOOK_LEVELS, now)}
                for subscriber in self._subscribers[ticker]:
                    subscriber.send(message)
        self._dirty_books.clear()

    def send_forming_candles(self) -> None:
        for ticker, subscribers in self._subscribers.items():
            market = self._projection.markets.get(ticker)
            if not subscribers or market is None or market.series is None:
                continue
            wanted = set().union(*(s.intervals for s in subscribers))
            for interval in wanted:
                self._send_candle(ticker, interval, market.series[interval].forming(), closed=False)

    async def run(self) -> None:
        ticks = 0
        while True:
            await asyncio.sleep(BOOK_EVERY_SECONDS)
            try:
                self.flush_books()
                ticks += 1
                if ticks % 4 == 0:  # every second
                    self.send_forming_candles()
            except Exception:
                logger.exception("hub flush failed")

    def _send_candle(self, ticker: str, interval: str, candle: Candle, *, closed: bool) -> None:
        message = {
            "type": "candle",
            "data": {"interval": interval, "closed": closed, "candle": candle_view(candle)},
        }
        for subscriber in self._subscribers[ticker]:
            if interval in subscriber.intervals:
                subscriber.send(message)
