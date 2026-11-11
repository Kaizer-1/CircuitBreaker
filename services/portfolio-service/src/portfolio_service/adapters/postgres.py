"""The PortfolioStore port on Postgres (database portfolio; ADR-0006).

One `transaction()` is one Postgres transaction. `lock=True` reads use SELECT ... FOR UPDATE, so two
settlements touching the same account wait for each other instead of overwriting each other.
"""

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from importlib import resources
from typing import Any

from psycopg import AsyncConnection, errors
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from portfolio_service.domain.model import (
    Account,
    AccountKind,
    Holding,
    NicknameTakenError,
    OrderType,
    Reservation,
    ReservationStatus,
    Side,
)
from portfolio_service.runtime.events import format_timestamp, parse_timestamp

logger = logging.getLogger("postgres")

MIGRATION_LOCK = 724_001  # pg_advisory_xact_lock key: one replica migrates at a time

# (read, locked read): FOR UPDATE holds a row lock until the transaction ends.
ACCOUNT = (
    "SELECT * FROM accounts WHERE trader_id = %s",
    "SELECT * FROM accounts WHERE trader_id = %s FOR UPDATE",
)
HOLDING = (
    "SELECT * FROM holdings WHERE trader_id = %s AND ticker = %s",
    "SELECT * FROM holdings WHERE trader_id = %s AND ticker = %s FOR UPDATE",
)
RESERVATION = (
    "SELECT * FROM reservations WHERE order_id = %s",
    "SELECT * FROM reservations WHERE order_id = %s FOR UPDATE",
)


def _ts(value: datetime | None) -> str | None:
    return format_timestamp(value) if value is not None else None


def _account(row: dict[str, Any]) -> Account:
    return Account(
        trader_id=str(row["trader_id"]),
        nickname=row["nickname"],
        kind=AccountKind(row["kind"]),
        cash_paise=row["cash_paise"],
        reserved_cash_paise=row["reserved_cash_paise"],
        starting_value_paise=row["starting_value_paise"],
        created_at=format_timestamp(row["created_at"]),
    )


def _holding(row: dict[str, Any]) -> Holding:
    return Holding(
        str(row["trader_id"]),
        row["ticker"],
        row["quantity"],
        row["reserved_quantity"],
        row["avg_cost_paise"],
    )


def _reservation(row: dict[str, Any]) -> Reservation:
    return Reservation(
        reservation_id=str(row["reservation_id"]),
        order_id=str(row["order_id"]),
        trader_id=str(row["trader_id"]),
        ticker=row["ticker"],
        side=Side(row["side"]),
        order_type=OrderType(row["order_type"]),
        quantity=row["quantity"],
        limit_price_paise=row["limit_price_paise"],
        protection_price_paise=row["protection_price_paise"],
        reserved_cash_paise=row["reserved_cash_paise"],
        reserved_quantity=row["reserved_quantity"],
        remaining_cash_paise=row["remaining_cash_paise"],
        remaining_quantity=row["remaining_quantity"],
        status=ReservationStatus(row["status"]),
        confirmed=row["confirmed"],
        expiry_requested_at=_ts(row["expiry_requested_at"]),
        created_at=format_timestamp(row["created_at"]),
    )


CHANGES_CHANNEL = "portfolio_changes"  # docs/03-architecture/08-read-side.md section 3


class PgTx:
    def __init__(self, conn: AsyncConnection[dict[str, Any]]) -> None:
        self._conn = conn

    async def _one(self, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
        cur = await self._conn.execute(sql, params)
        return await cur.fetchone()

    async def _all(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        cur = await self._conn.execute(sql, params)
        return await cur.fetchall()

    async def account(self, trader_id: str, *, lock: bool = False) -> Account | None:
        row = await self._one(ACCOUNT[lock], (trader_id,))
        return _account(row) if row else None

    async def account_by_nickname(self, nickname: str) -> Account | None:
        row = await self._one("SELECT * FROM accounts WHERE nickname = %s", (nickname,))
        return _account(row) if row else None

    async def insert_account(self, account: Account) -> None:
        try:
            async with self._conn.transaction():  # a savepoint: the outer transaction survives
                await self._conn.execute(
                    "INSERT INTO accounts (trader_id, nickname, kind, cash_paise,"
                    " reserved_cash_paise, starting_value_paise, created_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        account.trader_id,
                        account.nickname,
                        account.kind.value,
                        account.cash_paise,
                        account.reserved_cash_paise,
                        account.starting_value_paise,
                        parse_timestamp(account.created_at),
                    ),
                )
        except errors.UniqueViolation as exc:
            raise NicknameTakenError(account.nickname) from exc

    async def save_account(self, account: Account) -> None:
        await self._conn.execute(
            "UPDATE accounts SET cash_paise = %s, reserved_cash_paise = %s WHERE trader_id = %s",
            (account.cash_paise, account.reserved_cash_paise, account.trader_id),
        )

    async def holding(self, trader_id: str, ticker: str, *, lock: bool = False) -> Holding:
        row = await self._one(HOLDING[lock], (trader_id, ticker))
        return _holding(row) if row else Holding(trader_id, ticker)

    async def holdings(self, trader_id: str) -> list[Holding]:
        rows = await self._all(
            "SELECT * FROM holdings WHERE trader_id = %s ORDER BY ticker", (trader_id,)
        )
        return [_holding(r) for r in rows]

    async def save_holding(self, holding: Holding) -> None:
        await self._conn.execute(
            "INSERT INTO holdings (trader_id, ticker, quantity, reserved_quantity, avg_cost_paise)"
            " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (trader_id, ticker) DO UPDATE SET"
            " quantity = EXCLUDED.quantity, reserved_quantity = EXCLUDED.reserved_quantity,"
            " avg_cost_paise = EXCLUDED.avg_cost_paise",
            (
                holding.trader_id,
                holding.ticker,
                holding.quantity,
                holding.reserved_quantity,
                holding.avg_cost_paise,
            ),
        )

    async def reservation(self, order_id: str, *, lock: bool = False) -> Reservation | None:
        row = await self._one(RESERVATION[lock], (order_id,))
        return _reservation(row) if row else None

    async def insert_reservation(self, r: Reservation) -> None:
        await self._conn.execute(
            "INSERT INTO reservations (reservation_id, order_id, trader_id, ticker, side,"
            " order_type, quantity, limit_price_paise, protection_price_paise, reserved_cash_paise,"
            " reserved_quantity, remaining_cash_paise, remaining_quantity, status, confirmed,"
            " expiry_requested_at, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                r.reservation_id,
                r.order_id,
                r.trader_id,
                r.ticker,
                r.side.value,
                r.order_type.value,
                r.quantity,
                r.limit_price_paise,
                r.protection_price_paise,
                r.reserved_cash_paise,
                r.reserved_quantity,
                r.remaining_cash_paise,
                r.remaining_quantity,
                r.status.value,
                r.confirmed,
                parse_timestamp(r.expiry_requested_at) if r.expiry_requested_at else None,
                parse_timestamp(r.created_at),
            ),
        )

    async def save_reservation(self, r: Reservation) -> None:
        await self._conn.execute(
            "UPDATE reservations SET remaining_cash_paise = %s, remaining_quantity = %s,"
            " status = %s, confirmed = %s, expiry_requested_at = %s WHERE order_id = %s",
            (
                r.remaining_cash_paise,
                r.remaining_quantity,
                r.status.value,
                r.confirmed,
                parse_timestamp(r.expiry_requested_at) if r.expiry_requested_at else None,
                r.order_id,
            ),
        )

    async def unconfirmed_reservations(
        self, created_before: str, asked_before: str
    ) -> list[Reservation]:
        rows = await self._all(
            "SELECT * FROM reservations WHERE status = 'ACTIVE' AND NOT confirmed"
            " AND created_at <= %s"
            " AND (expiry_requested_at IS NULL OR expiry_requested_at <= %s) ORDER BY created_at",
            (parse_timestamp(created_before), parse_timestamp(asked_before)),
        )
        return [_reservation(r) for r in rows]

    async def standings(self, kinds: Sequence[AccountKind]) -> list[tuple[Account, list[Holding]]]:
        accounts = await self._all(
            "SELECT * FROM accounts WHERE kind = ANY(%s)", ([k.value for k in kinds],)
        )
        holdings = await self._all(
            "SELECT h.* FROM holdings h JOIN accounts a USING (trader_id)"
            " WHERE a.kind = ANY(%s) AND h.quantity > 0",
            ([k.value for k in kinds],),
        )
        by_trader: dict[str, list[Holding]] = {}
        for row in holdings:
            holding = _holding(row)
            by_trader.setdefault(holding.trader_id, []).append(holding)
        return [(a, by_trader.get(a.trader_id, [])) for a in map(_account, accounts)]

    async def last_prices(self) -> dict[str, int]:
        rows = await self._all("SELECT ticker, price_paise FROM last_prices", ())
        return {r["ticker"]: r["price_paise"] for r in rows}

    async def set_last_price(self, ticker: str, price_paise: int, at: str) -> None:
        await self._conn.execute(
            "INSERT INTO last_prices (ticker, price_paise, updated_at) VALUES (%s, %s, %s)"
            " ON CONFLICT (ticker) DO UPDATE SET price_paise = EXCLUDED.price_paise,"
            " updated_at = EXCLUDED.updated_at",
            (ticker, price_paise, parse_timestamp(at)),
        )

    async def add_settlement(
        self,
        *,
        trade_id: str,
        trader_id: str,
        order_id: str,
        ticker: str,
        side: str,
        quantity: int,
        price_paise: int,
        executed_at: str,
    ) -> None:
        await self._conn.execute(
            "INSERT INTO settlements (trade_id, trader_id, order_id, ticker, side, quantity,"
            " price_paise, executed_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                trade_id,
                trader_id,
                order_id,
                ticker,
                side,
                quantity,
                price_paise,
                parse_timestamp(executed_at),
            ),
        )

    async def mark_processed(self, event_id: str, event_type: str) -> bool:
        row = await self._one(
            "INSERT INTO processed_events (event_id, event_type) VALUES (%s, %s)"
            " ON CONFLICT (event_id) DO NOTHING RETURNING event_id",
            (event_id, event_type),
        )
        return row is not None

    async def notify(self, payload: str) -> None:
        await self._conn.execute("SELECT pg_notify(%s, %s)", (CHANGES_CHANNEL, payload))


class PostgresPortfolioStore:
    def __init__(self, pool: AsyncConnectionPool[AsyncConnection[dict[str, Any]]]) -> None:
        self._pool = pool

    @classmethod
    async def open(
        cls, conninfo: str, *, min_size: int = 1, max_size: int = 10
    ) -> "PostgresPortfolioStore":
        pool: AsyncConnectionPool[AsyncConnection[dict[str, Any]]] = AsyncConnectionPool(
            conninfo,
            min_size=min_size,
            max_size=max_size,
            kwargs={"row_factory": dict_row},
            open=False,
        )
        await pool.open(wait=True, timeout=10)
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[PgTx]:
        async with self._pool.connection() as conn, conn.transaction():
            yield PgTx(conn)

    async def migrate(self) -> None:
        """Apply every migrations/NNN_*.sql not applied yet, each in its own transaction."""
        folder = resources.files("portfolio_service") / "migrations"
        files = sorted(
            (f for f in folder.iterdir() if f.name.endswith(".sql")), key=lambda f: f.name
        )
        async with self._pool.connection() as conn:
            for file in files:
                async with conn.transaction():
                    await conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK,))
                    await conn.execute(
                        "CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY,"
                        " applied_at timestamptz NOT NULL DEFAULT now())"
                    )
                    cur = await conn.execute(
                        "SELECT 1 FROM schema_migrations WHERE version = %s", (file.name,)
                    )
                    if await cur.fetchone():
                        continue
                    await conn.execute(file.read_text())
                    await conn.execute(
                        "INSERT INTO schema_migrations (version) VALUES (%s)", (file.name,)
                    )
                    logger.info("migration applied", extra={"version": file.name})
