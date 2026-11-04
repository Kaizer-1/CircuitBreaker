"""The public API as the bots use it (through api-gateway, like any client): A-03, A-05, A-07,
Q-02, Q-04, Q-07, Q-09 and the Q-10 portfolio stream."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

from bot_traders.domain.market import Place


@dataclass(frozen=True)
class Session:
    account: str
    trader_id: str
    token: str


class OrderRefusedError(Exception):
    """The exchange answered 422: the order broke a rule (should never happen: see Ledger)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


class Exchange(Protocol):
    async def login(self, account: str) -> Session: ...

    async def place(self, session: Session, order: Place) -> dict[str, Any]: ...

    async def cancel(self, session: Session, order_id: str) -> None:
        """Ask to cancel; an order that is already finished is not an error."""

    async def open_orders(self, session: Session) -> list[dict[str, Any]]: ...

    async def portfolio(self, session: Session) -> dict[str, Any]: ...

    async def depth(self, session: Session, ticker: str) -> dict[str, Any]: ...

    async def instruments(self, session: Session) -> list[dict[str, Any]]: ...

    def portfolio_stream(self, session: Session) -> AsyncIterator[dict[str, Any]]:
        """Q-10 messages (`portfolio`, `fill`, `order_closed`) until the connection drops."""
        ...
