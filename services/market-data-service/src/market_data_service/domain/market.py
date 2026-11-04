"""Everything market-data shows for one ticker, kept as of one matching.v1 `sequence`.

Events are applied strictly in sequence order (the projection drops repeats and fills gaps), so
the state after sequence N is always the same, however the events arrived.
"""

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from market_data_service.domain.candles import INTERVALS, Candle, CandleSeries
from market_data_service.domain.depth import DepthBook, Level
from market_data_service.domain.instruments import Instrument

RECENT_TRADES = 200  # kept in memory; Q-05 and the WebSocket snapshot show up to 200 and 50


@dataclass(frozen=True)
class Trade:
    trade_id: str
    sequence: int
    price_paise: int
    quantity: int
    aggressor_side: str
    executed_at_ms: int


@dataclass(frozen=True)
class DayStats:
    """Today's figures (UTC day) from the trades of that day."""

    day: date
    open_paise: int
    high_paise: int
    low_paise: int
    volume: int


@dataclass
class Update:
    """What one event (or one clock tick) changed, for storage and live clients."""

    trade: Trade | None = None
    book_changed: bool = False
    candles: list[tuple[str, Candle, bool]] = field(
        default_factory=list
    )  # interval, candle, closed


def utc_day(at_ms: int) -> date:
    return datetime.fromtimestamp(at_ms / 1000, UTC).date()


class TickerMarket:
    def __init__(
        self,
        instrument: Instrument,
        *,
        sequence: int = 0,
        as_of_ms: int | None = None,
        last_price_paise: int | None = None,
        last_trade_at_ms: int | None = None,
        day: DayStats | None = None,
        depth: DepthBook | None = None,
        series: dict[str, CandleSeries] | None = None,
        recent_trades: Iterable[Trade] = (),
    ) -> None:
        self.instrument = instrument
        self.ticker = instrument.ticker
        self.sequence = sequence  # last matching.v1 event applied
        self.as_of_ms = as_of_ms
        self.last_trade_price: int | None = last_price_paise
        self.last_trade_at_ms = last_trade_at_ms
        self.day = day
        self.depth = depth or DepthBook()
        self.series = series  # created by the first event or clock tick
        self.recent: deque[Trade] = deque(recent_trades, maxlen=RECENT_TRADES)  # newest first

    @property
    def last_price_paise(self) -> int:
        return self.last_trade_price or self.instrument.reference_price_paise

    def apply(
        self, event_type: str, data: Mapping[str, Any], at_ms: int, *, depth: bool = True
    ) -> Update:
        """Apply one matching.v1 event. `at_ms` is its own time (`executed_at` for a trade).
        `depth=False` when the depth already includes it (built from a later snapshot)."""
        update = Update(book_changed=depth and self.depth.apply(event_type, data))
        self.sequence = int(data["sequence"])
        self.as_of_ms = at_ms
        if event_type == "TradeExecuted":
            trade = Trade(
                trade_id=data["trade_id"],
                sequence=self.sequence,
                price_paise=int(data["price_paise"]),
                quantity=int(data["quantity"]),
                aggressor_side=data["aggressor_side"],
                executed_at_ms=at_ms,
            )
            series = self._series(trade.executed_at_ms)
            self._record(trade)
            for interval, s in series.items():
                for candle, closed in s.add_trade(
                    trade.price_paise, trade.quantity, trade.executed_at_ms
                ):
                    update.candles.append((interval, candle, closed))
            update.trade = trade
        else:
            update.candles = self.close_until(at_ms)  # event time moved on
        return update

    def close_until(self, now_ms: int) -> list[tuple[str, Candle, bool]]:
        """Close every candle that ended at or before `now_ms` (the clock, when no trade did)."""
        return [
            (interval, candle, True)
            for interval, s in self._series(now_ms).items()
            for candle in s.close_until(now_ms)
        ]

    def start_candles(self, at_ms: int) -> None:
        """Begin the candle series now if no event has yet (flat from service start)."""
        self._series(at_ms)

    def depth_levels(self, levels: int) -> tuple[list[Level], list[Level]]:
        return self.depth.depth(levels)

    def day_view(self, today: date) -> DayStats:
        """Today's figures; flat at the last price if nothing traded today."""
        if self.day is not None and self.day.day == today:
            return self.day
        last = self.last_price_paise
        return DayStats(today, last, last, last, 0)

    def _series(self, at_ms: int) -> dict[str, CandleSeries]:
        if self.series is None:
            # Before the first trade the flat price is the reference price (event catalog).
            self.series = {
                name: CandleSeries(ms, at_ms, self.last_price_paise)
                for name, ms in INTERVALS.items()
            }
        return self.series

    def _record(self, trade: Trade) -> None:
        self.last_trade_price = trade.price_paise
        self.last_trade_at_ms = trade.executed_at_ms
        self.recent.appendleft(trade)
        day, price = utc_day(trade.executed_at_ms), trade.price_paise
        d = self.day
        if d is None or d.day != day:
            self.day = DayStats(day, price, price, price, trade.quantity)
        else:
            self.day = DayStats(
                day,
                d.open_paise,
                max(d.high_paise, price),
                min(d.low_paise, price),
                d.volume + trade.quantity,
            )
