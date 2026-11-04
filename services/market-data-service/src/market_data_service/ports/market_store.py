"""Where the read models live (Cassandra keyspace market_data)."""

from dataclasses import dataclass, field
from typing import Protocol

from market_data_service.domain.candles import Candle
from market_data_service.domain.market import DayStats, Trade


@dataclass(frozen=True)
class StoredMarket:
    """One ticker as last saved."""

    sequence: int
    as_of_ms: int | None
    last_price_paise: int | None
    last_trade_at_ms: int | None
    day: DayStats | None
    candles: dict[str, list[Candle]]  # per interval, oldest first, the newest KEEP_BUCKETS
    recent_trades: list[Trade]  # newest first


@dataclass
class Changes:
    """Everything one or more events changed for a ticker: written in one logged batch, so the
    stored `sequence` always matches the stored candles and trades."""

    ticker: str
    sequence: int = 0
    as_of_ms: int | None = None
    last_price_paise: int | None = None
    last_trade_at_ms: int | None = None
    day: DayStats | None = None
    candles: dict[tuple[str, int], Candle] = field(default_factory=dict)  # (interval, bucket)
    trades: list[Trade] = field(default_factory=list)


class MarketStore(Protocol):
    async def migrate(self) -> None: ...

    async def load(self, ticker: str) -> StoredMarket | None: ...

    async def save(self, changes: Changes) -> None: ...

    async def candles(
        self, ticker: str, interval: str, from_ms: int, to_ms: int, limit: int
    ) -> list[Candle]:
        """The newest `limit` candles with a bucket start in [from_ms, to_ms], oldest first."""
        ...

    async def trades(self, ticker: str, limit: int) -> list[Trade]:
        """The newest `limit` trades, newest first."""
        ...
