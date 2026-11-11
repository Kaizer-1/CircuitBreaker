"""In-memory fakes. A transaction works on a copy and replaces the state only on commit, so an
exception inside it rolls back, as in Postgres. Uniqueness rules match the real schema."""

import copy
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import Any

from portfolio_service.domain.model import (
    Account,
    AccountKind,
    Holding,
    NicknameTakenError,
    Reservation,
    ReservationStatus,
)
from portfolio_service.runtime.events import parse_timestamp


@dataclass
class State:
    accounts: dict[str, Account] = field(default_factory=dict)
    holdings: dict[tuple[str, str], Holding] = field(default_factory=dict)
    reservations: dict[str, Reservation] = field(default_factory=dict)
    last_prices: dict[str, int] = field(default_factory=dict)
    settlements: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    processed: set[str] = field(default_factory=set)


class InMemoryTx:
    def __init__(self, state: State) -> None:
        self.s = state
        self.notifications: list[str] = []  # delivered on commit, like Postgres NOTIFY

    async def notify(self, payload: str) -> None:
        self.notifications.append(payload)

    async def account(self, trader_id: str, *, lock: bool = False) -> Account | None:
        a = self.s.accounts.get(trader_id)
        return replace(a) if a else None

    async def account_by_nickname(self, nickname: str) -> Account | None:
        found = [a for a in self.s.accounts.values() if a.nickname == nickname]
        return replace(found[0]) if found else None

    async def insert_account(self, account: Account) -> None:
        if any(a.nickname == account.nickname for a in self.s.accounts.values()):
            raise NicknameTakenError(account.nickname)
        self.s.accounts[account.trader_id] = replace(account)

    async def save_account(self, account: Account) -> None:
        assert account.cash_paise >= account.reserved_cash_paise >= 0, (
            account
        )  # the CHECK constraint
        self.s.accounts[account.trader_id] = replace(account)

    async def holding(self, trader_id: str, ticker: str, *, lock: bool = False) -> Holding:
        h = self.s.holdings.get((trader_id, ticker))
        return replace(h) if h else Holding(trader_id, ticker)

    async def holdings(self, trader_id: str) -> list[Holding]:
        return [replace(h) for (t, _), h in sorted(self.s.holdings.items()) if t == trader_id]

    async def save_holding(self, holding: Holding) -> None:
        assert holding.quantity >= holding.reserved_quantity >= 0, holding  # the CHECK constraint
        self.s.holdings[(holding.trader_id, holding.ticker)] = replace(holding)

    async def reservation(self, order_id: str, *, lock: bool = False) -> Reservation | None:
        r = self.s.reservations.get(order_id)
        return replace(r) if r else None

    async def insert_reservation(self, reservation: Reservation) -> None:
        assert reservation.order_id not in self.s.reservations  # UNIQUE (order_id)
        self.s.reservations[reservation.order_id] = replace(reservation)

    async def save_reservation(self, reservation: Reservation) -> None:
        self.s.reservations[reservation.order_id] = replace(reservation)

    async def unconfirmed_reservations(
        self, created_before: str, asked_before: str
    ) -> list[Reservation]:
        cut, asked = parse_timestamp(created_before), parse_timestamp(asked_before)
        return [
            replace(r)
            for r in self.s.reservations.values()
            if r.status is ReservationStatus.ACTIVE
            and not r.confirmed
            and (r.expiry_requested_at is None or parse_timestamp(r.expiry_requested_at) <= asked)
            and parse_timestamp(r.created_at) <= cut
        ]

    async def standings(self, kinds: Sequence[AccountKind]) -> list[tuple[Account, list[Holding]]]:
        return [
            (
                copy.deepcopy(a),
                [
                    copy.deepcopy(h)
                    for (t, _), h in self.s.holdings.items()
                    if t == a.trader_id and h.quantity > 0
                ],
            )
            for a in self.s.accounts.values()
            if a.kind in kinds
        ]

    async def last_prices(self) -> dict[str, int]:
        return dict(self.s.last_prices)

    async def set_last_price(self, ticker: str, price_paise: int, at: str) -> None:
        self.s.last_prices[ticker] = price_paise

    async def add_settlement(self, **row: Any) -> None:
        key = (row["trade_id"], row["trader_id"])
        assert key not in self.s.settlements  # PRIMARY KEY (trade_id, trader_id)
        self.s.settlements[key] = row

    async def mark_processed(self, event_id: str, event_type: str) -> bool:
        if event_id in self.s.processed:
            return False
        self.s.processed.add(event_id)
        return True


class InMemoryPortfolioStore:
    def __init__(self) -> None:
        self.state = State()
        self.before_commit: Callable[[], None] = lambda: None  # raise here to fail a commit
        self.notified: list[str] = []
        self.listeners: list[Callable[[str], Awaitable[None]]] = []  # every replica's LISTEN

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[InMemoryTx]:
        work = copy.deepcopy(self.state)
        tx = InMemoryTx(work)
        yield tx
        self.before_commit()
        self.state = work
        self.notified += tx.notifications
        for listener in self.listeners:
            for payload in tx.notifications:
                await listener(payload)


class FakeOrderService:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, str]] = []
        self.down = False

    async def request_expiry(self, order_id: str, trader_id: str, ticker: str) -> None:
        if self.down:
            raise ConnectionError("order-service is down")
        self.requests.append((order_id, trader_id, ticker))
