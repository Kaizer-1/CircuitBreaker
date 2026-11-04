"""The MarketStore port on Cassandra (keyspace market_data, migrations/001_market_data.cql).

One logged batch per save, so the stored sequence, candles and trades always agree. The driver
is synchronous, so every call runs in a worker thread.
"""

import asyncio
import calendar
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from importlib import resources
from typing import Any
from uuid import UUID

from market_data_service.domain.candles import INTERVALS, KEEP_BUCKETS, Candle
from market_data_service.domain.market import DayStats, Trade, utc_day
from market_data_service.ports.market_store import Changes, StoredMarket

RESTORE_TRADES = 200
TRADE_DAYS_BACK = 7  # Q-05 looks this many days back for trades when today has too few

QUERIES = {
    "select_market": "SELECT * FROM last_prices WHERE ticker = ?",
    "upsert_market": "INSERT INTO last_prices (ticker, sequence, last_price_paise, last_trade_at,"
    " day, day_open_paise, day_high_paise, day_low_paise, day_volume, updated_at)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    "upsert_candle": "INSERT INTO candles (ticker, interval, bucket_start, open_paise, high_paise,"
    " low_paise, close_paise, volume, trade_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
    "select_candles": "SELECT * FROM candles WHERE ticker = ? AND interval = ? LIMIT ?",
    "select_candle_range": "SELECT * FROM candles WHERE ticker = ? AND interval = ?"
    " AND bucket_start >= ? AND bucket_start <= ? LIMIT ?",
    "insert_trade": "INSERT INTO trades_by_ticker (ticker, day, executed_at, trade_id, sequence,"
    " price_paise, quantity, aggressor_side) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
    "select_trades": "SELECT * FROM trades_by_ticker WHERE ticker = ? AND day = ? LIMIT ?",
}


def connect_cassandra(
    contact_points: Sequence[str], port: int, keyspace: str, username: str, password: str
) -> Any:
    """A session on this service's own keyspace, logged in with this service's own role."""
    from cassandra.auth import PlainTextAuthProvider
    from cassandra.cluster import Cluster
    from cassandra.io.asyncioreactor import AsyncioConnection

    cluster = Cluster(
        list(contact_points),
        port=port,
        auth_provider=PlainTextAuthProvider(username, password),
        connection_class=AsyncioConnection,
        # v4, not v5: under concurrent load, v5's segment framing failed its CRC with the asyncio
        # reactor (CrcException, connection defunct, host marked down: seen with the Phase 3 bots).
        protocol_version=4,
        connect_timeout=5,
    )
    return cluster.connect(keyspace)


def migration_statements() -> list[str]:
    statements: list[str] = []
    folder = resources.files("market_data_service") / "migrations"
    for file in sorted(
        (f for f in folder.iterdir() if f.name.endswith(".cql")), key=lambda f: f.name
    ):
        text = "\n".join(
            line for line in file.read_text().splitlines() if not line.strip().startswith("--")
        )
        statements += [s.strip() for s in text.split(";") if s.strip()]
    return statements


def to_dt(at_ms: int) -> datetime:
    return datetime.fromtimestamp(at_ms / 1000, UTC)


def to_ms(value: datetime) -> int:
    """Cassandra returns naive UTC datetimes with millisecond precision."""
    return calendar.timegm(value.utctimetuple()) * 1000 + value.microsecond // 1000


def to_date(value: Any) -> date:
    """The driver returns its own Date type for a `date` column."""
    if isinstance(value, date):
        return value
    result: date = value.date()
    return result


class CassandraMarketStore:
    def __init__(self, session: Any) -> None:
        self._session = session
        self._prepared: dict[str, Any] = {}

    async def _run(self, name: str, *values: Any) -> list[Any]:
        def call() -> list[Any]:
            if name not in self._prepared:
                self._prepared[name] = self._session.prepare(QUERIES[name])
            return list(self._session.execute(self._prepared[name], values))

        return await asyncio.to_thread(call)

    async def migrate(self) -> None:
        for statement in migration_statements():
            await asyncio.to_thread(self._session.execute, statement)

    async def load(self, ticker: str) -> StoredMarket | None:
        rows = await self._run("select_market", ticker)
        if not rows:
            return None
        row = rows[0]
        candles = {}
        for interval in INTERVALS:
            stored = await self._run("select_candles", ticker, interval, KEEP_BUCKETS)
            candles[interval] = [_candle(r) for r in reversed(stored)]
        day = None
        if row.day is not None:
            day = DayStats(
                to_date(row.day),
                row.day_open_paise,
                row.day_high_paise,
                row.day_low_paise,
                row.day_volume,
            )
        last_trade_at = to_ms(row.last_trade_at) if row.last_trade_at is not None else None
        recent = await self._recent(ticker, RESTORE_TRADES, last_trade_at)
        return StoredMarket(
            sequence=row.sequence,
            as_of_ms=to_ms(row.updated_at) if row.updated_at is not None else None,
            last_price_paise=row.last_price_paise,
            last_trade_at_ms=last_trade_at,
            day=day,
            candles=candles,
            recent_trades=recent,
        )

    async def save(self, changes: Changes) -> None:
        def call() -> None:
            from cassandra.query import BatchStatement, BatchType

            batch = BatchStatement(batch_type=BatchType.LOGGED)
            for name in ("upsert_market", "upsert_candle", "insert_trade"):
                if name not in self._prepared:
                    self._prepared[name] = self._session.prepare(QUERIES[name])
            d = changes.day
            batch.add(
                self._prepared["upsert_market"],
                (
                    changes.ticker,
                    changes.sequence,
                    changes.last_price_paise,
                    to_dt(changes.last_trade_at_ms) if changes.last_trade_at_ms else None,
                    d.day if d else None,
                    d.open_paise if d else None,
                    d.high_paise if d else None,
                    d.low_paise if d else None,
                    d.volume if d else None,
                    to_dt(changes.as_of_ms) if changes.as_of_ms is not None else None,
                ),
            )
            for (interval, bucket), c in changes.candles.items():
                batch.add(
                    self._prepared["upsert_candle"],
                    (
                        changes.ticker,
                        interval,
                        to_dt(bucket),
                        c.open_paise,
                        c.high_paise,
                        c.low_paise,
                        c.close_paise,
                        c.volume,
                        c.trade_count,
                    ),
                )
            for t in changes.trades:
                batch.add(
                    self._prepared["insert_trade"],
                    (
                        changes.ticker,
                        utc_day(t.executed_at_ms),
                        to_dt(t.executed_at_ms),
                        UUID(t.trade_id),
                        t.sequence,
                        t.price_paise,
                        t.quantity,
                        t.aggressor_side,
                    ),
                )
            self._session.execute(batch)

        await asyncio.to_thread(call)

    async def candles(
        self, ticker: str, interval: str, from_ms: int, to_ms: int, limit: int
    ) -> list[Candle]:
        rows = await self._run(
            "select_candle_range", ticker, interval, to_dt(from_ms), to_dt(to_ms), limit
        )
        return [_candle(r) for r in reversed(rows)]

    async def trades(self, ticker: str, limit: int) -> list[Trade]:
        rows = await self._run("select_market", ticker)
        last = rows[0].last_trade_at if rows else None
        return await self._recent(ticker, limit, to_ms(last) if last is not None else None)

    async def _recent(self, ticker: str, limit: int, last_trade_at_ms: int | None) -> list[Trade]:
        if last_trade_at_ms is None:
            return []
        found: list[Trade] = []
        day = utc_day(last_trade_at_ms)
        for _ in range(TRADE_DAYS_BACK):
            rows = await self._run("select_trades", ticker, day, limit - len(found))
            found += sorted((_trade(r) for r in rows), key=lambda t: -t.sequence)
            if len(found) >= limit:
                break
            day -= timedelta(days=1)
        return found


def _candle(r: Any) -> Candle:
    return Candle(
        to_ms(r.bucket_start),
        r.open_paise,
        r.high_paise,
        r.low_paise,
        r.close_paise,
        r.volume,
        r.trade_count,
    )


def _trade(r: Any) -> Trade:
    return Trade(
        str(r.trade_id),
        r.sequence,
        r.price_paise,
        r.quantity,
        r.aggressor_side,
        to_ms(r.executed_at),
    )
