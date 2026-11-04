"""The market projection: matching.v1 events in, candles, depth, trades and last prices out.

Rules (docs/03-architecture/08-read-side.md):

- **Strict sequence order per ticker.** An event at or below the last applied sequence is a
  duplicate and is skipped. An event further ahead means some were missed: they are fetched from
  matching-engine's event store (Q-14) first, so the projection never has a gap.
- **Start-up = catch-up from the event store.** Depth starts from the engine's latest snapshot
  (Q-15); candles, trades and last prices from what this service saved; the events after both
  are read from Q-14. The live feed is positioned at the end of every partition *before* the
  catch-up, so nothing published meanwhile is lost (repeats are dropped by sequence).
- **Every change is saved before the next event** (one logged batch per event: its candles, its
  trade and the new sequence), so a restart resumes exactly where the store says.
- **Candles close by event time, or by the clock** (`tick`) when nothing trades, but only while
  the feed is caught up; a trade that arrives later for a closed bucket corrects it.
- `CandleClosed` is best effort: if Redpanda is down it is logged and dropped (bots act on the
  next one); the stored candle is what Q-03 serves.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol

from market_data_service.domain.candles import INTERVALS, Candle, CandleSeries
from market_data_service.domain.depth import DepthBook
from market_data_service.domain.instruments import INSTRUMENTS, TICKERS
from market_data_service.domain.market import TickerMarket, Trade, Update
from market_data_service.events.candles import TOPIC as CANDLES_TOPIC
from market_data_service.events.candles import candle_closed
from market_data_service.ports.event_store import EventStore
from market_data_service.ports.market_store import Changes, MarketStore, StoredMarket
from market_data_service.ports.publisher import Publisher
from market_data_service.runtime.events import EventEnvelope, format_timestamp, parse_timestamp

logger = logging.getLogger("projection")

FEED_TOPIC = "matching.v1"
CLOSE_GRACE_MS = 500  # the clock closes a bucket only 0.5 s after it ended
CATCH_UP_BATCH = 200  # events per saved batch while catching up
PUBLISH_ATTEMPT_SECONDS = 2.0


class MarketListener(Protocol):
    """Live clients (the WebSocket hub)."""

    def on_trade(self, ticker: str, trade: Trade) -> None: ...

    def on_book(self, ticker: str) -> None: ...

    def on_candle(self, ticker: str, interval: str, candle: Candle, closed: bool) -> None: ...


class _NoListener:
    def on_trade(self, ticker: str, trade: Trade) -> None:
        return None

    def on_book(self, ticker: str) -> None:
        return None

    def on_candle(self, ticker: str, interval: str, candle: Candle, closed: bool) -> None:
        return None


def event_time_ms(envelope: EventEnvelope) -> int:
    """A trade's own time is `executed_at`; every other event's is `occurred_at`."""
    raw = envelope.data.get("executed_at") if envelope.event_type == "TradeExecuted" else None
    return int(parse_timestamp(str(raw or envelope.occurred_at)).timestamp() * 1000)


class MarketProjection:
    def __init__(
        self,
        store: MarketStore,
        events: EventStore,
        publisher: Publisher,
        *,
        environment: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        feed_caught_up: Callable[[], bool] = lambda: True,
        on_gap: Callable[[], None] = lambda: None,
        on_closed: Callable[[str], None] = lambda interval: None,
    ) -> None:
        self._store = store
        self._events = events
        self._publisher = publisher
        self._environment = environment
        self._clock = clock
        self._sleep = sleep
        self._feed_caught_up = feed_caught_up
        self._on_gap = on_gap
        self._on_closed = on_closed
        self._lock = asyncio.Lock()
        self.listener: MarketListener = _NoListener()
        self.markets: dict[str, TickerMarket] = {}
        self.ready = False  # every ticker caught up from the event store

    def now_ms(self) -> int:
        return int(self._clock().timestamp() * 1000)

    # ------------------------------------------------------------------ start-up

    async def start(self) -> None:
        """Load what was saved and catch up from the event store, ticker by ticker."""
        for ticker in TICKERS:
            self.markets[ticker] = await self._catch_up(ticker)
            self.markets[ticker].start_candles(self.now_ms())  # flat candles from now on
        self.ready = True
        logger.info(
            "projection caught up",
            extra={"sequences": {t: m.sequence for t, m in self.markets.items()}},
        )

    async def _catch_up(self, ticker: str) -> TickerMarket:
        stored = await self._store.load(ticker)
        snapshot = await self._events.snapshot(ticker, self.now_ms())
        market = restore(ticker, stored, DepthBook.from_snapshot(snapshot.bids, snapshot.asks))
        changes = Changes(ticker)
        count = 0
        async for envelope in self._events.events_after(
            ticker, min(snapshot.sequence, market.sequence)
        ):
            sequence = int(envelope.data["sequence"])
            if sequence > market.sequence:
                update = market.apply(
                    envelope.event_type,
                    envelope.data,
                    event_time_ms(envelope),
                    depth=sequence > snapshot.sequence,
                )
                _collect(changes, market, update)
                count += 1
                if count % CATCH_UP_BATCH == 0:
                    await self._save(changes)
                    changes = Changes(ticker)
            elif sequence > snapshot.sequence:
                market.depth.apply(envelope.event_type, envelope.data)
        if count % CATCH_UP_BATCH:
            await self._save(changes)
        return market

    # ------------------------------------------------------------------ live events

    async def handle(self, envelope: EventEnvelope) -> None:
        """One matching.v1 event from the live feed."""
        ticker = envelope.data.get("ticker")
        if ticker not in INSTRUMENTS or not isinstance(envelope.data.get("sequence"), int):
            await self._dead_letter(envelope, "no valid ticker or sequence")
            return
        async with self._lock:
            market = self.markets[ticker]
            sequence = int(envelope.data["sequence"])
            if sequence <= market.sequence:
                return  # a repeat (or already read from the event store)
            if sequence > market.sequence + 1:
                logger.warning(
                    "gap in matching.v1: reading the missing events from the event store",
                    extra={"ticker": ticker, "have": market.sequence, "got": sequence},
                )
                self._on_gap()
                async for missed in self._events.events_after(ticker, market.sequence):
                    if int(missed.data["sequence"]) > market.sequence:
                        await self._apply(market, missed)
                if sequence <= market.sequence:
                    return
            await self._apply(market, envelope)

    async def _apply(self, market: TickerMarket, envelope: EventEnvelope) -> None:
        sequence = int(envelope.data["sequence"])
        try:
            update = market.apply(envelope.event_type, envelope.data, event_time_ms(envelope))
        except (KeyError, TypeError, ValueError) as exc:
            # A malformed event can never be applied; skipping it keeps the stream moving.
            await self._dead_letter(envelope, f"{type(exc).__name__}: {exc}")
            market.sequence = sequence
            update = Update()
        changes = Changes(market.ticker)
        _collect(changes, market, update)
        await self._save(changes)
        self._notify(market.ticker, update)
        await self._publish_closed(market.ticker, update)

    # ------------------------------------------------------------------ the clock

    async def tick(self) -> None:
        """Close the candles that ended more than 0.5 s ago, if the feed is caught up (otherwise
        a trade for them may still be on its way)."""
        if not self.ready or not self._feed_caught_up():
            return
        now = self.now_ms() - CLOSE_GRACE_MS
        async with self._lock:
            for market in self.markets.values():
                update = Update(candles=market.close_until(now))
                if not update.candles:
                    continue
                changes = Changes(market.ticker)
                _collect(changes, market, update)
                await self._save(changes)
                self._notify(market.ticker, update)
                await self._publish_closed(market.ticker, update)

    async def run_clock(self, every_seconds: float = 0.25) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("candle clock failed")
            await self._sleep(every_seconds)

    # ------------------------------------------------------------------ helpers

    async def _save(self, changes: Changes) -> None:
        """Retried until the store accepts it: the next event must not be applied before."""
        delay = 0.2
        while True:
            try:
                await self._store.save(changes)
                return
            except Exception as exc:
                logger.warning("saving the read model failed, retrying", extra={"error": repr(exc)})
                await self._sleep(delay)
                delay = min(delay * 2, 5.0)

    def _notify(self, ticker: str, update: Update) -> None:
        if update.trade is not None:
            self.listener.on_trade(ticker, update.trade)
        if update.book_changed:
            self.listener.on_book(ticker)
        for interval, candle, closed in update.candles:
            self.listener.on_candle(ticker, interval, candle, closed)

    async def _publish_closed(self, ticker: str, update: Update) -> None:
        for interval, candle, closed in update.candles:
            if not closed:
                continue
            self._on_closed(interval)
            event = candle_closed(
                ticker, interval, candle, environment=self._environment, at=self._clock()
            )
            try:
                await asyncio.wait_for(
                    self._publisher.publish(CANDLES_TOPIC, ticker, event), PUBLISH_ATTEMPT_SECONDS
                )
            except Exception as exc:
                logger.warning(
                    "CandleClosed not published", extra={"ticker": ticker, "error": repr(exc)}
                )
                return  # Redpanda is down: skip the rest of this batch too

    async def _dead_letter(self, envelope: EventEnvelope, error: str) -> None:
        logger.error(
            "matching.v1 event cannot be applied: sent to the dead-letter topic",
            extra={"event_id": envelope.event_id, "error": error},
        )
        body = {
            "original_topic": FEED_TOPIC,
            "consumer": "market-data-service",
            "error": error,
            "failed_at": format_timestamp(self._clock()),
            "value": envelope.to_json().decode(),
        }
        try:
            await asyncio.wait_for(
                self._publisher.publish_raw(f"{FEED_TOPIC}.dlq", None, json.dumps(body).encode()),
                PUBLISH_ATTEMPT_SECONDS,
            )
        except Exception as exc:
            logger.warning("dead letter not published", extra={"error": repr(exc)})


def restore(ticker: str, stored: StoredMarket | None, depth: DepthBook) -> TickerMarket:
    instrument = INSTRUMENTS[ticker]
    if stored is None:
        return TickerMarket(instrument, depth=depth)
    series = (
        {i: CandleSeries.restore(INTERVALS[i], stored.candles[i]) for i in INTERVALS}
        if all(stored.candles.get(i) for i in INTERVALS)
        else None
    )
    return TickerMarket(
        instrument,
        sequence=stored.sequence,
        as_of_ms=stored.as_of_ms,
        last_price_paise=stored.last_price_paise,
        last_trade_at_ms=stored.last_trade_at_ms,
        day=stored.day,
        depth=depth,
        series=series,
        recent_trades=stored.recent_trades,
    )


def _collect(changes: Changes, market: TickerMarket, update: Update) -> None:
    changes.sequence = market.sequence
    changes.as_of_ms = market.as_of_ms
    changes.last_price_paise = market.last_trade_price
    changes.last_trade_at_ms = market.last_trade_at_ms
    changes.day = market.day
    for interval, candle, _ in update.candles:
        changes.candles[(interval, candle.bucket_start_ms)] = candle
    if update.trade is not None:
        changes.trades.append(update.trade)
