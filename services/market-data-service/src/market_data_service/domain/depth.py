"""Order-book depth: a projection of matching.v1 (docs/03-architecture/08-read-side.md).

`OrderAccepted` of a LIMIT order adds it at its limit price, `TradeExecuted` sets both orders'
remaining quantity (0 removes it) and `OrderCancelled` removes what is left. A MARKET order never
rests, so it never shows. Only quantity and order count per price are exposed: trader identities
never leave this module.

**Never a crossed book.** The engine handles one command at a time: an incoming order is accepted
first and its trades follow, so for a moment the projection holds an incoming buy priced at or
above the best ask (or a sell at or below the best bid). Depth leaves that incoming order out
while it still crosses; once its trades are applied it is gone, or rests without crossing.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Level:
    price_paise: int
    quantity: int
    order_count: int


@dataclass
class _Order:
    side: str
    price: int
    remaining: int


class DepthBook:
    def __init__(self) -> None:
        self._orders: dict[str, _Order] = {}
        self._levels: dict[str, dict[int, list[int]]] = {"BUY": {}, "SELL": {}}  # [qty, count]
        self._incoming: str | None = None  # the order of the command being applied

    @classmethod
    def from_snapshot(
        cls, bids: Sequence[Mapping[str, Any]], asks: Sequence[Mapping[str, Any]]
    ) -> "DepthBook":
        """From matching-engine's BookState (Q-15): price levels holding resting orders."""
        book = cls()
        for side, levels in (("BUY", bids), ("SELL", asks)):
            for level in levels:
                for o in level["orders"]:
                    book._add(o["order_id"], side, level["price_paise"], o["remaining_quantity"])
        return book

    def apply(self, event_type: str, data: Mapping[str, Any]) -> bool:
        """Apply one matching.v1 event. True if the depth may have changed."""
        if event_type == "OrderAccepted":
            self._incoming = data["order_id"]
            if data["order_type"] == "LIMIT":
                self._add(
                    data["order_id"], data["side"], data["limit_price_paise"], data["quantity"]
                )
                return True
            return False
        if event_type == "TradeExecuted":
            self._set_remaining(data["buy_order_id"], data["buy_order_remaining_quantity"])
            self._set_remaining(data["sell_order_id"], data["sell_order_remaining_quantity"])
            return True
        if event_type == "OrderCancelled":
            return self._set_remaining(data["order_id"], 0)
        return False  # OrderCancelRejected, OrderExpired: never in the book

    def depth(self, levels: int) -> tuple[list[Level], list[Level]]:
        """Bids high to low and asks low to high, `levels` per side, never crossed."""
        bids = {p: list(v) for p, v in self._levels["BUY"].items()}
        asks = {p: list(v) for p, v in self._levels["SELL"].items()}
        incoming = self._orders.get(self._incoming) if self._incoming else None
        if incoming is not None:
            own, other = (bids, asks) if incoming.side == "BUY" else (asks, bids)
            crosses = bool(other) and (
                min(other) <= incoming.price
                if incoming.side == "BUY"
                else max(other) >= incoming.price
            )
            if crosses:
                level = own[incoming.price]
                level[0] -= incoming.remaining
                level[1] -= 1
                if level[1] == 0:
                    del own[incoming.price]
        return (
            [Level(p, *bids[p]) for p in sorted(bids, reverse=True)[:levels]],
            [Level(p, *asks[p]) for p in sorted(asks)[:levels]],
        )

    def resting_orders(self) -> int:
        return len(self._orders)

    def _add(self, order_id: str, side: str, price: int, quantity: int) -> None:
        self._orders[order_id] = _Order(side, price, quantity)
        level = self._levels[side].setdefault(price, [0, 0])
        level[0] += quantity
        level[1] += 1

    def _set_remaining(self, order_id: str, remaining: int) -> bool:
        order = self._orders.get(order_id)
        if order is None:
            return False  # a MARKET order, or one that already left the book
        level = self._levels[order.side][order.price]
        level[0] -= order.remaining - remaining
        order.remaining = remaining
        if remaining == 0:
            del self._orders[order_id]
            level[1] -= 1
            if level[1] == 0:
                del self._levels[order.side][order.price]
        return True
