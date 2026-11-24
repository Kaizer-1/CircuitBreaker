"""The portfolio database (Postgres `portfolio`). Everything one use case does happens in one Tx."""

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from portfolio_service.domain.model import Account, AccountKind, Holding, Reservation


@dataclass(frozen=True)
class DatabaseFault:
    """chaos's "database degraded" fault, stored in this database (application/faults.py)."""

    fault: str  # DB_DEGRADED
    delay_ms: int
    started_at: datetime


class Tx(Protocol):
    """One database transaction. `lock=True` reads take a row lock until commit."""

    async def account(self, trader_id: str, *, lock: bool = False) -> Account | None: ...

    async def account_by_nickname(self, nickname: str) -> Account | None: ...

    async def insert_account(self, account: Account) -> None:
        """Raises NicknameTakenError if the nickname is already used."""

    async def save_account(self, account: Account) -> None: ...

    async def holding(self, trader_id: str, ticker: str, *, lock: bool = False) -> Holding:
        """The holding, or an empty one (quantity 0) that save_holding will create."""

    async def holdings(self, trader_id: str) -> list[Holding]: ...

    async def save_holding(self, holding: Holding) -> None: ...

    async def reservation(self, order_id: str, *, lock: bool = False) -> Reservation | None: ...

    async def insert_reservation(self, reservation: Reservation) -> None: ...

    async def save_reservation(self, reservation: Reservation) -> None: ...

    async def unconfirmed_reservations(
        self, created_before: str, asked_before: str
    ) -> list[Reservation]:
        """ACTIVE, no OrderAccepted seen, created before `created_before`, and no expiry asked for
        yet or last asked for before `asked_before`."""

    async def standings(self, kinds: Sequence[AccountKind]) -> list[tuple[Account, list[Holding]]]:
        """Every account of these kinds with its holdings (the leaderboard's input, Q-11)."""

    async def last_prices(self) -> dict[str, int]: ...

    async def set_last_price(self, ticker: str, price_paise: int, at: str) -> None: ...

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
    ) -> None: ...

    async def mark_processed(self, event_id: str, event_type: str) -> bool:
        """Record the event as processed. False if it already was (a duplicate)."""

    async def wait_in_database(self, seconds: float) -> None:
        """Make Postgres itself spend `seconds` in this transaction (chaos: database degraded)."""

    async def database_fault(self) -> DatabaseFault | None: ...

    async def set_database_fault(self, fault: DatabaseFault) -> bool:
        """Store the fault. False if one is already stored."""

    async def clear_database_fault(self) -> bool:
        """Remove the fault. False if there was none."""

    async def notify(self, payload: str) -> None:
        """Tell every replica about a change once this transaction commits (Postgres NOTIFY on
        channel `portfolio_changes`; nothing is sent if it rolls back)."""


class PortfolioStore(Protocol):
    def transaction(self) -> AbstractAsyncContextManager[Tx]:
        """Commit on normal exit, roll back if the block raises."""
