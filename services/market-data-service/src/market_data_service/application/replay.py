"""Replay (Q-13): play a past stretch of the market at a chosen speed, as frames.

docs/03-architecture/04-event-sourcing-and-cqrs.md, section 5, and api/README.md (Q-13):

1. Rebuild the book at `from` exactly as time travel does (snapshot + events up to `from`) and send
   it as the first frame.
2. Walk the stored events after it up to `to`. The events of one command (same `causation_id`)
   are applied together and sent as one frame, so a viewer never sees half a match. The frame
   lists every fill of that command (`trades`, oldest first), so the replay's tape is exactly the
   one the live market showed. Before each frame the replay waits the original time between the
   events divided by the speed.
3. When market time reaches `to`, send `end` with the hash of the book at `to` (equal to Q-12's
   at `to`), then close.

The viewer can pause, resume and change speed at any time. A replay paused for longer than the
limit is closed, so an abandoned tab does not hold a place forever. Like time travel, the book is
a private copy: nothing here reaches the live projection, its consumers or its sockets.
"""

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Any

from market_data_service.application.time_travel import TimeTravel, candle_up_to, ms_of
from market_data_service.application.views import candle_view, iso, order_book_view, trade_view
from market_data_service.domain.candles import INTERVALS, Candle, bucket_of
from market_data_service.domain.instruments import INSTRUMENTS
from market_data_service.domain.market import Trade
from market_data_service.runtime.events import EventEnvelope

SPEEDS = (1, 2, 5, 10, 60)
MAX_WINDOW_MS = 30 * 60_000  # api/README.md: a replay window is at most 30 minutes
CANDLE_MS = INTERVALS["5s"]  # the frame's forming candle
DEPTH_LEVELS = 10

Send = Callable[[dict[str, Any]], Awaitable[None]]


class PausedTooLongError(Exception):
    """The viewer left the replay paused for longer than the limit."""


class Pacer:
    """Market time in a replay: it runs at `speed` times the wall clock and stops while paused."""

    def __init__(
        self,
        start_ms: int,
        speed: int,
        *,
        pause_limit_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._anchor_ms = float(start_ms)
        self._anchor_wall = clock()
        self.speed = speed
        self.paused = False
        self._paused_at = 0.0
        self._pause_limit = pause_limit_seconds
        self._changed = asyncio.Event()

    def now_ms(self) -> float:
        if self.paused:
            return self._anchor_ms
        return self._anchor_ms + (self._clock() - self._anchor_wall) * 1000 * self.speed

    def _restart_from_now(self) -> None:
        self._anchor_ms, self._anchor_wall = self.now_ms(), self._clock()

    def pause(self) -> None:
        if not self.paused:
            self._restart_from_now()
            self.paused, self._paused_at = True, self._clock()
            self._changed.set()

    def resume(self) -> None:
        if self.paused:
            self._anchor_wall = self._clock()
            self.paused = False
            self._changed.set()

    def set_speed(self, speed: int) -> None:
        self._restart_from_now()
        self.speed = speed
        self._changed.set()

    async def wait_until(self, target_ms: float) -> None:
        """Return when market time reaches `target_ms`; react at once to pause, resume, speed."""
        while True:
            self._changed.clear()
            if self.paused:
                timeout = self._pause_limit - (self._clock() - self._paused_at)
                if timeout <= 0:
                    raise PausedTooLongError
            else:
                remaining = target_ms - self.now_ms()
                if remaining <= 0:
                    return
                timeout = remaining / 1000 / self.speed
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), timeout)


def _trade(event: EventEnvelope) -> Trade:
    d = event.data
    return Trade(
        d["trade_id"],
        int(d["sequence"]),
        int(d["price_paise"]),
        int(d["quantity"]),
        d["aggressor_side"],
        ms_of(d["executed_at"]),
    )


def _roll(candle: Candle, at_ms: int) -> Candle:
    """The forming candle at `at_ms`: a new bucket starts flat at the last close."""
    start = bucket_of(at_ms, CANDLE_MS)
    if start == candle.bucket_start_ms:
        return candle
    c = candle.close_paise
    return Candle(start, c, c, c, c, 0, 0)


def _add(candle: Candle, trade: Trade) -> Candle:
    p = trade.price_paise
    if not candle.trade_count:
        return Candle(candle.bucket_start_ms, p, p, p, p, trade.quantity, 1)
    return Candle(
        candle.bucket_start_ms,
        candle.open_paise,
        max(candle.high_paise, p),
        min(candle.low_paise, p),
        p,
        candle.volume + trade.quantity,
        candle.trade_count + 1,
    )


async def _groups(events: Any) -> Any:
    """Consecutive events with the same causation_id: one command's effects."""
    group: list[EventEnvelope] = []
    async for event in events:
        if group and event.causation_id != group[0].causation_id:
            yield group
            group = []
        group.append(event)
    if group:
        yield group


class ReplaySession:
    def __init__(
        self,
        time_travel: TimeTravel,
        *,
        ticker: str,
        from_ms: int,
        to_ms: int,
        speed: int,
        pause_limit_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._time_travel = time_travel
        self._events = time_travel.events
        self._store = time_travel.store
        self.ticker = ticker
        self.from_ms, self.to_ms = from_ms, to_ms
        self.pacer = Pacer(from_ms, speed, pause_limit_seconds=pause_limit_seconds, clock=clock)
        self.frames_sent = 0

    async def run(self, send: Send) -> None:
        book = await self._time_travel.rebuild(self.ticker, self.from_ms)
        latest = await self._store.trades_until(self.ticker, self.from_ms, 1)
        last_trade = latest[0] if latest else None
        last_price = (
            last_trade.price_paise if last_trade else INSTRUMENTS[self.ticker].reference_price_paise
        )
        start = bucket_of(self.from_ms, CANDLE_MS)
        so_far = await self._store.trades_between(self.ticker, start, self.from_ms)
        candle = candle_up_to(start, so_far, last_price)

        async def frame(at: str, trade: Trade | None, fills: list[Trade]) -> None:
            bids, asks = book.depth(DEPTH_LEVELS)
            await send(
                {
                    "type": "frame",
                    "data": {
                        "as_of": at,
                        "as_of_sequence": book.sequence,
                        "order_book": order_book_view(
                            self.ticker, book.sequence, book.as_of or at, bids, asks
                        ),
                        "last_trade": trade_view(trade) if trade else None,
                        "trades": [trade_view(t) for t in fills],
                        "candle": candle_view(candle),
                    },
                }
            )
            self.frames_sent += 1

        await frame(iso(self.from_ms), last_trade, [])
        later = self._events.events_after(self.ticker, book.sequence, until_ms=self.to_ms)
        async for group in _groups(later):
            at_ms = ms_of(group[0].occurred_at)
            await self.pacer.wait_until(at_ms)
            candle = _roll(candle, at_ms)
            fills: list[Trade] = []
            for event in group:
                book.apply(event.event_type, event.data, event.occurred_at)
                if event.event_type == "TradeExecuted":
                    fills.append(_trade(event))
                    candle = _add(candle, fills[-1])
            await frame(group[0].occurred_at, fills[-1] if fills else None, fills)
        await self.pacer.wait_until(self.to_ms)
        await send(
            {
                "type": "end",
                "data": {
                    "frames_sent": self.frames_sent,
                    "from": iso(self.from_ms),
                    "to": iso(self.to_ms),
                    "as_of_sequence": book.sequence,
                    "state_hash": await asyncio.to_thread(book.state_hash),
                },
            }
        )
