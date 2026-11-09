"""Time travel (Q-12): the market exactly as it was at time T, rebuilt from the event store.

docs/03-architecture/04-event-sourcing-and-cqrs.md, section 5:

1. **The book.** Take matching-engine's nearest snapshot taken at or before T (Q-15), then apply
   the stored events after it up to T (Q-14) to a fresh `ReplayBook`. Apply only, never decide.
   Snapshots come every 500 events per ticker, so this is at most about 500 events wherever T is.
2. **The proof.** The rebuilt book's `state_hash` is computed by the same rule as the engine's
   live Q-16 hash, so two books are equal exactly when their hashes are. When T is at or after the
   live book's last event ("now"), the live book *is* the market at T: the engine's Q-16 is read
   first, the rebuild stops at exactly that sequence, and the answer says whether the two hashes
   match (`verification`). Comparing at one sequence means the market need not be quiet: bots can
   keep trading while the proof runs.
3. **Candles and trades** up to T come from this service's own read models. The 1-minute candle
   that holds T is recomputed from the trades up to T, so nothing that happened after T leaks in.

All of it is a private copy for this one request: the live projection, its consumers and its
sockets are never touched, so time travel cannot change the live market.
"""

import asyncio
from dataclasses import dataclass

from market_data_service.domain.candles import INTERVALS, Candle, bucket_of
from market_data_service.domain.instruments import INSTRUMENTS
from market_data_service.domain.market import Trade
from market_data_service.domain.replay_book import ReplayBook
from market_data_service.ports.event_store import EventStore, LiveBook, LiveBooks
from market_data_service.ports.market_store import MarketStore
from market_data_service.runtime.events import parse_timestamp

CANDLES = 60  # 1-minute candles up to T (TimeTravelView)
RECENT_TRADES = 50
CANDLE_INTERVAL = "1m"


@dataclass(frozen=True)
class MarketAt:
    ticker: str
    at_ms: int
    book: ReplayBook
    state_hash: str
    last_price_paise: int
    candles: list[Candle]  # oldest first
    recent_trades: list[Trade]  # newest first
    verification: "Verification | None" = None


@dataclass(frozen=True)
class Verification:
    """The rebuilt book against the engine's live book at the same sequence."""

    live_sequence: int
    live_state_hash: str
    matches: bool


def candle_up_to(bucket_start_ms: int, trades: list[Trade], previous_close: int) -> Candle:
    """The candle of one bucket from the trades so far (oldest first); flat if there are none."""
    if not trades:
        p = previous_close
        return Candle(bucket_start_ms, p, p, p, p, 0, 0)
    prices = [t.price_paise for t in trades]
    return Candle(
        bucket_start_ms,
        prices[0],
        max(prices),
        min(prices),
        prices[-1],
        sum(t.quantity for t in trades),
        len(trades),
    )


def ms_of(stamp: str) -> int:
    return int(parse_timestamp(stamp).timestamp() * 1000)


def comparable(live: LiveBook | None, at_ms: int) -> bool:
    """Is the live book the market at `at_ms`? Yes if nothing has happened since `at_ms`."""
    return live is not None and (live.sequence == 0 or at_ms >= ms_of(live.as_of))


class TimeTravel:
    def __init__(
        self, events: EventStore, store: MarketStore, live: LiveBooks | None = None
    ) -> None:
        self._events = events
        self._store = store
        self._live = live

    async def rebuild(
        self, ticker: str, at_ms: int, *, until_sequence: int | None = None
    ) -> ReplayBook:
        """The whole book after every event at or before `at_ms` (and `until_sequence`)."""
        snapshot = await self._events.snapshot(ticker, at_ms)
        book = ReplayBook.from_snapshot(
            ticker,
            sequence=snapshot.sequence,
            as_of=snapshot.as_of,
            bids=snapshot.bids,
            asks=snapshot.asks,
            closed=snapshot.closed,
        )
        async for event in self._events.events_after(ticker, book.sequence, until_ms=at_ms):
            if until_sequence is not None and int(event.data["sequence"]) > until_sequence:
                break
            book.apply(event.event_type, event.data, event.occurred_at)
        return book

    async def market_at(self, ticker: str, at_ms: int, *, verify: bool = False) -> MarketAt:
        """The market at `at_ms`. With `verify`, and T at or after the live book's last event,
        also compare with the engine's live book (read first: its sequence bounds the rebuild)."""
        live = await self._live.live_book(ticker) if verify and self._live else None
        if not comparable(live, at_ms):
            live = None
        book = await self.rebuild(ticker, at_ms, until_sequence=live.sequence if live else None)
        # Hashing thousands of finished orders takes milliseconds: not on the event loop.
        state_hash = await asyncio.to_thread(book.state_hash)
        verification = (
            Verification(
                live.sequence,
                live.state_hash,
                book.sequence == live.sequence and state_hash == live.state_hash,
            )
            if live
            else None
        )
        recent = await self._store.trades_until(ticker, at_ms, RECENT_TRADES)
        last_price = recent[0].price_paise if recent else INSTRUMENTS[ticker].reference_price_paise
        return MarketAt(
            ticker=ticker,
            at_ms=at_ms,
            book=book,
            state_hash=state_hash,
            last_price_paise=last_price,
            candles=await self._candles(ticker, at_ms, last_price),
            recent_trades=recent,
            verification=verification,
        )

    async def _candles(self, ticker: str, at_ms: int, last_price: int) -> list[Candle]:
        step = INTERVALS[CANDLE_INTERVAL]
        current = bucket_of(at_ms, step)
        stored = await self._store.candles(
            ticker, CANDLE_INTERVAL, current - (CANDLES - 1) * step, current, CANDLES
        )
        earlier = [c for c in stored if c.bucket_start_ms < current]
        if len(earlier) == len(stored):
            return earlier  # no candle held T: the service was not running then
        # The stored candle for T's minute includes trades after T. With no trade in it up to T,
        # the newest trade at or before T came earlier, so its price is the previous close.
        so_far = await self._store.trades_between(ticker, current, at_ms)
        return [*earlier, candle_up_to(current, so_far, last_price)]
