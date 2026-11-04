"""The live portfolio (Q-10, api/README.md): `portfolio` on connect and after every change, `fill`
for each of the trader's trades, `order_closed` when an order is done.

portfolio-service runs several replicas, and a phone's socket is on one of them while the trade
that changes its portfolio may be settled by another. So every use case that changes a portfolio
adds a **change** to its transaction (`Tx.notify`, a Postgres NOTIFY, delivered only on commit);
every replica listens and the one holding the trader's socket pushes. It re-reads the portfolio
after the commit, so a phone never sees a change that was rolled back
(docs/03-architecture/08-read-side.md section 3).

A trade also moves a price: sockets whose portfolio holds that ticker are re-valued, at most once
a second each.
"""

import asyncio
import json
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("live")

QUEUE_LIMIT = 200
REVALUE_EVERY_SECONDS = 1.0

Message = dict[str, Any]


@dataclass(frozen=True)
class Change:
    """What one committed transaction changed: a trader's portfolio, a ticker's price, or both."""

    trader_id: str | None = None
    ticker: str | None = None  # its last price moved
    messages: tuple[Message, ...] = ()  # `fill` / `order_closed` for that trader

    def to_json(self) -> str:
        return json.dumps(
            {"trader_id": self.trader_id, "ticker": self.ticker, "messages": list(self.messages)},
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str) -> "Change":
        body = json.loads(raw)
        return cls(body.get("trader_id"), body.get("ticker"), tuple(body.get("messages") or ()))


def fill_message(d: dict[str, Any], side: str) -> Message:
    """One trader's side of a TradeExecuted."""
    buy = side == "BUY"
    return {
        "type": "fill",
        "data": {
            "order_id": d["buy_order_id"] if buy else d["sell_order_id"],
            "trade_id": d["trade_id"],
            "ticker": d["ticker"],
            "side": side,
            "price_paise": d["price_paise"],
            "quantity": d["quantity"],
            "order_remaining_quantity": d[
                "buy_order_remaining_quantity" if buy else "sell_order_remaining_quantity"
            ],
            "executed_at": d["executed_at"],
        },
    }


def order_closed_message(order_id: str, status: str, reason: str | None) -> Message:
    return {
        "type": "order_closed",
        "data": {"order_id": order_id, "status": status, "cancel_reason": reason},
    }


@dataclass(eq=False)
class Subscriber:
    trader_id: str
    queue: asyncio.Queue[Message] = field(default_factory=lambda: asyncio.Queue(QUEUE_LIMIT))
    held: frozenset[str] = frozenset()  # tickers in the last portfolio sent
    lagging: bool = False

    def send(self, message: Message) -> None:
        if self.lagging:
            return
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            self.lagging = True  # the socket is closed; the phone reconnects for a fresh view


class PortfolioHub:
    def __init__(self, read_portfolio: Callable[[str], Awaitable[dict[str, Any]]]) -> None:
        self._read = read_portfolio
        self._subscribers: dict[str, set[Subscriber]] = defaultdict(set)
        self._moved: set[str] = set()  # tickers whose price moved since the last re-valuation

    def connections(self) -> int:
        return sum(len(s) for s in self._subscribers.values())

    def subscribe(self, trader_id: str) -> Subscriber:
        subscriber = Subscriber(trader_id)
        self._subscribers[trader_id].add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        self._subscribers[subscriber.trader_id].discard(subscriber)

    async def view(self, subscriber: Subscriber) -> Message:
        """The current portfolio as a `portfolio` message (on connect)."""
        portfolio = await self._read(subscriber.trader_id)
        subscriber.held = _held(portfolio)
        return {"type": "portfolio", "data": portfolio}

    async def on_change(self, raw: str) -> None:
        """A NOTIFY from any replica (including this one)."""
        change = Change.from_json(raw)
        if change.ticker:
            self._moved.add(change.ticker)
        if change.trader_id and self._subscribers[change.trader_id]:
            for message in change.messages:
                for subscriber in self._subscribers[change.trader_id]:
                    subscriber.send(message)
            await self.refresh(change.trader_id)

    async def refresh(self, trader_id: str) -> None:
        subscribers = list(self._subscribers[trader_id])
        if not subscribers:
            return
        portfolio = await self._read(trader_id)
        for subscriber in subscribers:
            subscriber.held = _held(portfolio)
            subscriber.send({"type": "portfolio", "data": portfolio})

    async def refresh_all(self) -> None:
        """After the listener reconnected: a NOTIFY may have been missed meanwhile."""
        for trader_id in [t for t, subs in self._subscribers.items() if subs]:
            await self.refresh(trader_id)

    async def revalue(self) -> None:
        """Re-value every socket holding a ticker whose price moved (called once a second)."""
        moved, self._moved = self._moved, set()
        if not moved:
            return
        for trader_id, subscribers in list(self._subscribers.items()):
            if any(s.held & moved for s in subscribers):
                await self.refresh(trader_id)

    async def run(self) -> None:
        while True:
            await asyncio.sleep(REVALUE_EVERY_SECONDS)
            try:
                await self.revalue()
            except Exception:
                logger.exception("re-valuation failed")


def _held(portfolio: dict[str, Any]) -> frozenset[str]:
    return frozenset(h["ticker"] for h in portfolio.get("holdings", ()) if h["quantity"] > 0)
