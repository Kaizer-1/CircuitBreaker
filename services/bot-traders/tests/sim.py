"""A simulated exchange for the bots, and virtual time to run it fast.

`SimExchange` answers the public API the way the real services do: order-service's validation
(A-05), portfolio-service's reservations (no margin, no short selling, a MARKET buy reserves
quantity × (last + 10%, rounded up to the tick)), and a price-time matching engine (trades at the
resting price, MARKET orders limited to ±10% of the last price, self-trade prevention cancelling
the incoming order). Anything the real exchange would refuse is recorded in `refusals`.
"""

import asyncio
import heapq
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from itertools import count
from typing import Any

from bot_traders.domain.market import (
    REFERENCE_PRICES,
    Place,
    buy_protection,
    floor_tick,
    valid_shape,
)
from bot_traders.ports.exchange import OrderRefusedError, Session

BOT_CASH = 50_000_000
BOT_SHARES = 1_000


class VirtualTime:
    """asyncio sleeps that wake in virtual-time order when the driver advances the clock."""

    def __init__(self) -> None:
        self.now = 0.0
        self._heap: list[tuple[float, int, asyncio.Future[None]]] = []
        self._seq = count()

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._heap, (self.now + max(seconds, 0.0), next(self._seq), future))
        await future

    async def settle(self) -> None:
        for _ in range(20):
            await asyncio.sleep(0)

    async def run_until(self, until: float) -> None:
        await self.settle()
        while self._heap and self._heap[0][0] <= until:
            wake, _, future = heapq.heappop(self._heap)
            self.now = wake
            future.set_result(None)
            await self.settle()
        self.now = until


@dataclass
class Resting:
    order_id: str
    trader: str
    side: str
    price: int
    remaining: int


def _each(quantity: int) -> dict[str, int]:
    return dict.fromkeys(REFERENCE_PRICES, quantity)


@dataclass
class Account:
    cash: int = BOT_CASH
    reserved_cash: int = 0
    shares: dict[str, int] = field(default_factory=lambda: _each(BOT_SHARES))
    reserved_shares: dict[str, int] = field(default_factory=lambda: _each(0))


@dataclass
class Order:
    order_id: str
    trader: str
    ticker: str
    side: str
    order_type: str
    quantity: int
    limit: int | None
    reserved_cash: int
    remaining: int
    per_share: int = 0  # cash reserved per share of a BUY, fixed when it was placed
    status: str = "OPEN"


class SimExchange:
    def __init__(self, clock: VirtualTime) -> None:
        self.time = clock
        self.accounts: dict[str, Account] = {}
        self.orders: dict[str, Order] = {}
        self.books: dict[str, dict[str, list[Resting]]] = {
            t: {"BUY": [], "SELL": []} for t in REFERENCE_PRICES
        }
        self.last = dict(REFERENCE_PRICES)
        self.trades: list[tuple[str, int, int, float]] = []  # ticker, price, quantity, time
        self.placed: list[tuple[str, float, Place]] = []  # account, time, order
        self.refusals: list[tuple[str, str, Place]] = []
        self.requests = 0
        self.order_lists = 0  # Q-07 calls (they scan the order history: keep them rare)
        self.streams: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self._ids = count(1)

    # ------------------------------------------------------------------ the API

    async def login(self, account: str) -> Session:
        self.accounts.setdefault(account, Account())
        return Session(account, account, f"token-{account}")

    async def instruments(self, session: Session) -> list[dict[str, Any]]:
        return [{"ticker": t, "last_price_paise": p} for t, p in self.last.items()]

    async def depth(self, session: Session, ticker: str) -> dict[str, Any]:
        self.requests += 1
        bid, ask = self.best(ticker)
        return {"best_bid_paise": bid, "best_ask_paise": ask}

    async def portfolio(self, session: Session) -> dict[str, Any]:
        self.requests += 1
        a = self.accounts[session.account]
        return {
            "available_cash_paise": a.cash - a.reserved_cash,
            "holdings": [
                {
                    "ticker": t,
                    "quantity": a.shares[t],
                    "available_quantity": a.shares[t] - a.reserved_shares[t],
                }
                for t in REFERENCE_PRICES
            ],
        }

    async def portfolio_stream(self, session: Session) -> AsyncIterator[dict[str, Any]]:
        queue = self.streams.setdefault(session.account, asyncio.Queue())
        while True:
            yield await queue.get()

    def _push(self, account: str, message: dict[str, Any]) -> None:
        if account in self.streams:
            self.streams[account].put_nowait(message)

    async def open_orders(self, session: Session) -> list[dict[str, Any]]:
        self.requests += 1
        self.order_lists += 1
        return [
            {
                "order_id": o.order_id,
                "ticker": o.ticker,
                "side": o.side,
                "quantity": o.quantity,
                "remaining_quantity": o.remaining,
                "limit_price_paise": o.limit,
            }
            for o in self.orders.values()
            if o.trader == session.account and o.status == "OPEN"
        ]

    async def cancel(self, session: Session, order_id: str) -> None:
        self.requests += 1
        order = self.orders.get(order_id)
        if order is None or order.trader != session.account or order.status != "OPEN":
            return
        book = self.books[order.ticker][order.side]
        book[:] = [r for r in book if r.order_id != order_id]
        self._finish(order)

    async def place(self, session: Session, request: Place) -> dict[str, Any]:
        self.requests += 1
        account = self.accounts[session.account]
        refusal = valid_shape(request)
        ticker = request.ticker
        if refusal is None and request.side == "BUY":
            per_share = request.limit_price_paise or buy_protection(self.last[ticker])
            if request.quantity * per_share > account.cash - account.reserved_cash:
                refusal = "INSUFFICIENT_FUNDS"
        if refusal is None and request.side == "SELL":
            free = account.shares[ticker] - account.reserved_shares[ticker]
            if request.quantity > free:
                refusal = "INSUFFICIENT_SHARES"
        if refusal is not None:
            self.refusals.append((session.account, refusal, request))
            raise OrderRefusedError(refusal)
        self.placed.append((session.account, self.time.now, request))
        order_id = f"0dde0000-0000-4000-8000-{next(self._ids):012d}"
        reserved = per_share = 0
        if request.side == "BUY":
            per_share = request.limit_price_paise or buy_protection(self.last[ticker])
            reserved = request.quantity * per_share
            account.reserved_cash += reserved
        else:
            account.reserved_shares[ticker] += request.quantity
        order = Order(
            order_id,
            session.account,
            ticker,
            request.side,
            request.order_type,
            request.quantity,
            request.limit_price_paise,
            reserved,
            request.quantity,
            per_share,
        )
        self.orders[order_id] = order
        self._match(order)
        return {"order_id": order_id, "status": "PENDING"}

    # ------------------------------------------------------------------ the panic sell's calls

    async def market_sell(self, session: Session, ticker: str) -> int:
        """A-05 as the panic sends it: a MARKET sell of 1 share, its HTTP status."""
        try:
            await self.place(session, Place(ticker, "SELL", 1, None))
        except OrderRefusedError:
            return 422
        return 202

    async def list_orders(self, session: Session) -> int:
        self.requests += 1
        return 200

    # ------------------------------------------------------------------ the engine

    def best(self, ticker: str) -> tuple[int | None, int | None]:
        bids, asks = self.books[ticker]["BUY"], self.books[ticker]["SELL"]
        return (bids[0].price if bids else None, asks[0].price if asks else None)

    def _match(self, order: Order) -> None:
        ticker, side = order.ticker, order.side
        other = self.books[ticker]["SELL" if side == "BUY" else "BUY"]
        if order.limit is not None:
            limit = order.limit
        elif side == "BUY":
            limit = buy_protection(self.last[ticker])
        else:
            limit = floor_tick(self.last[ticker] * 0.9)
        while order.remaining and other:
            best = other[0]
            if best.price > limit if side == "BUY" else best.price < limit:
                break
            if best.trader == order.trader:
                break  # self-trade prevention: cancel the rest of the incoming order
            fill = min(order.remaining, best.remaining)
            self._settle(order, self.orders[best.order_id], best.price, fill)
            best.remaining -= fill
            if best.remaining == 0:
                other.pop(0)
        rests = order.remaining and order.limit is not None and order.status == "OPEN"
        if rests and (not other or not _crosses(order, other[0])):
            # (an own order at the top that still crosses: self-trade prevention cancels the rest)
            own = self.books[ticker][side]
            assert order.limit is not None
            own.append(Resting(order.order_id, order.trader, side, order.limit, order.remaining))
            own.sort(key=lambda r: -r.price if side == "BUY" else r.price)
            return
        if order.status == "OPEN":
            self._finish(order)

    def _settle(self, incoming: Order, resting: Order, price: int, quantity: int) -> None:
        buy, sell = (incoming, resting) if incoming.side == "BUY" else (resting, incoming)
        buyer, seller = self.accounts[buy.trader], self.accounts[sell.trader]
        cost = price * quantity
        buyer.reserved_cash -= buy.per_share * quantity
        buy.reserved_cash -= buy.per_share * quantity
        buyer.cash -= cost
        buyer.shares[buy.ticker] += quantity
        seller.reserved_shares[sell.ticker] -= quantity
        seller.shares[sell.ticker] -= quantity
        seller.cash += cost
        for order in (buy, sell):
            order.remaining -= quantity
            self._push(
                order.trader,
                {
                    "type": "fill",
                    "data": {
                        "order_id": order.order_id,
                        "order_remaining_quantity": order.remaining,
                    },
                },
            )
            if order.remaining == 0:
                self._finish(order)
        self.last[buy.ticker] = price
        self.trades.append((buy.ticker, price, quantity, self.time.now))
        for account in (buyer, seller):
            assert account.cash - account.reserved_cash >= 0, "no margin"
            assert all(account.shares[t] >= account.reserved_shares[t] >= 0 for t in account.shares)

    def _finish(self, order: Order) -> None:
        account = self.accounts[order.trader]
        if order.side == "BUY":
            account.reserved_cash -= order.reserved_cash
            order.reserved_cash = 0
        else:
            account.reserved_shares[order.ticker] -= order.remaining
        order.status = "DONE"
        self._push(order.trader, {"type": "order_closed", "data": {"order_id": order.order_id}})


def _crosses(order: Order, best: Resting) -> bool:
    assert order.limit is not None
    return best.price <= order.limit if order.side == "BUY" else best.price >= order.limit
