"""The portfolio database (Postgres `portfolio`). Everything one use case does happens in one Tx."""

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from portfolio_service.domain.model import Account, AccountKind, Holding, Reservation


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

    async def unconfirmed_reservations(self, created_before: str) -> list[Reservation]:
        """ACTIVE, no OrderAccepted seen, no expiry requested yet, created before the given time."""

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

    async def notify(self, payload: str) -> None:
        """Tell every replica about a change once this transaction commits (Postgres NOTIFY on
        channel `portfolio_changes`; nothing is sent if it rolls back)."""


class PortfolioStore(Protocol):
    def transaction(self) -> AbstractAsyncContextManager[Tx]:
        """Commit on normal exit, roll back if the block raises."""
