"""The whole order book of one ticker, rebuilt from stored events for time travel (Q-12, Q-13).

Apply only, never decide (docs/03-architecture/04-event-sourcing-and-cqrs.md, "Decide vs apply"):
the book starts from matching-engine's snapshot (Q-15) and applies the events after it (Q-14).
`OrderAccepted` adds an order, `TradeExecuted` reduces both orders (0 finishes them, FILLED),
`OrderCancelled` finishes what is left (CANCELLED), `OrderExpired` records a tombstone (EXPIRED),
and `OrderCancelRejected` changes nothing. There is no matching code here, so the past comes out
exactly as it was recorded, whatever the engine's rules are today.

Unlike the live depth (domain/depth.py) this keeps every resting order with its trader, time
priority and every finished order, because it must reproduce the engine's `state_hash` (Q-16) by
the rule in api/matching-engine.yaml. It is this service's own code: nothing is imported from
matching-engine, and a contract test checks the hash against the example the spec states.

Each instance is a private copy built for one request: nothing here is shared with the live
projection, so time travel can never change live state.
"""

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from market_data_service.domain.depth import Level


class CorruptHistoryError(Exception):
    """The stored events do not fit together (a gap, or an order that is not in the book)."""


@dataclass
class _Order:
    order_id: str
    trader_id: str
    side: str
    price: int | None  # None for a MARKET order, which is in the book only while it matches
    remaining: int
    accepted_sequence: int


class ReplayBook:
    def __init__(self, ticker: str) -> None:
        self.ticker = ticker
        self.sequence = 0  # last applied event
        self.as_of: str | None = None  # occurred_at of the last applied event
        self._orders: dict[str, _Order] = {}
        self._levels: dict[str, dict[int, list[_Order]]] = {"BUY": {}, "SELL": {}}
        self._closed: dict[str, tuple[str, str]] = {}  # order_id -> (state, trader_id)

    @classmethod
    def from_snapshot(
        cls,
        ticker: str,
        *,
        sequence: int,
        as_of: str | None,
        bids: Iterable[Mapping[str, Any]],
        asks: Iterable[Mapping[str, Any]],
        closed: Iterable[Mapping[str, Any]],
    ) -> "ReplayBook":
        """From a Q-15 BookState. Sequence 0 is the empty book before any event (as_of null)."""
        book = cls(ticker)
        book.sequence = sequence
        book.as_of = as_of if sequence else None
        for side, levels in (("BUY", bids), ("SELL", asks)):
            for level in levels:
                price = int(level["price_paise"])
                for o in level["orders"]:
                    order = _Order(
                        o["order_id"],
                        o["trader_id"],
                        side,
                        price,
                        int(o["remaining_quantity"]),
                        int(o["accepted_sequence"]),
                    )
                    book._rest(order, price)
        book._closed = {c["order_id"]: (c["state"], c["trader_id"]) for c in closed}
        return book

    # ------------------------------------------------------------------ apply

    def apply(self, event_type: str, data: Mapping[str, Any], occurred_at: str) -> None:
        sequence = int(data["sequence"])
        if sequence != self.sequence + 1:
            raise CorruptHistoryError(f"sequence {sequence} does not follow {self.sequence}")
        if event_type == "OrderAccepted":
            self._known_new(data["order_id"])
            order = _Order(
                data["order_id"],
                data["trader_id"],
                data["side"],
                data["limit_price_paise"] if data["order_type"] == "LIMIT" else None,
                int(data["quantity"]),
                sequence,
            )
            if order.price is None:
                self._orders[order.order_id] = order
            else:
                self._rest(order, order.price)
        elif event_type == "TradeExecuted":
            for key in ("buy", "sell"):
                order = self._live(data[f"{key}_order_id"])
                order.remaining -= int(data["quantity"])
                if order.remaining != int(data[f"{key}_order_remaining_quantity"]):
                    raise CorruptHistoryError(f"trade {data['trade_id']}: quantities do not add up")
                if order.remaining == 0:
                    self._finish(order, "FILLED")
        elif event_type == "OrderCancelled":
            order = self._live(data["order_id"])
            if int(data["cancelled_quantity"]) != order.remaining:
                raise CorruptHistoryError(f"cancel of {order.order_id}: quantity does not match")
            self._finish(order, "CANCELLED")
        elif event_type == "OrderExpired":
            self._known_new(data["order_id"])
            self._closed[data["order_id"]] = ("EXPIRED", data["trader_id"])
        elif event_type != "OrderCancelRejected":  # a rejection changes nothing
            raise CorruptHistoryError(f"unknown event type {event_type}")
        self.sequence = sequence
        self.as_of = occurred_at

    def _known_new(self, order_id: str) -> None:
        if order_id in self._orders or order_id in self._closed:
            raise CorruptHistoryError(f"order {order_id} is already known")

    def _live(self, order_id: str) -> _Order:
        try:
            return self._orders[order_id]
        except KeyError:
            raise CorruptHistoryError(f"order {order_id} is not in the book") from None

    def _rest(self, order: _Order, price: int) -> None:
        self._orders[order.order_id] = order
        self._levels[order.side].setdefault(price, []).append(order)

    def _finish(self, order: _Order, state: str) -> None:
        del self._orders[order.order_id]
        self._closed[order.order_id] = (state, order.trader_id)
        if order.price is None:
            return
        level = self._levels[order.side][order.price]
        level.remove(order)
        if not level:
            del self._levels[order.side][order.price]

    # ------------------------------------------------------------------ queries

    def _prices(self, side: str) -> list[int]:
        """Best first: bids high to low, asks low to high."""
        return sorted(self._levels[side], reverse=side == "BUY")

    def depth(self, levels: int) -> tuple[list[Level], list[Level]]:
        """Bids high to low and asks low to high, `levels` per side."""

        def side(name: str) -> list[Level]:
            return [
                Level(
                    price,
                    sum(o.remaining for o in self._levels[name][price]),
                    len(self._levels[name][price]),
                )
                for price in self._prices(name)[:levels]
            ]

        return side("BUY"), side("SELL")

    def canonical(self) -> dict[str, Any]:
        """The book in the canonical form of api/matching-engine.yaml (BookState.state_hash)."""

        def side(name: str) -> list[Any]:
            return [
                [
                    price,
                    [
                        [o.order_id, o.trader_id, o.remaining, o.accepted_sequence]
                        for o in self._levels[name][price]
                    ],
                ]
                for price in self._prices(name)
            ]

        if any(o.price is None for o in self._orders.values()):
            raise CorruptHistoryError("a market order is still being matched")
        return {
            "ticker": self.ticker,
            "sequence": self.sequence,
            "as_of": self.as_of,
            "bids": side("BUY"),
            "asks": side("SELL"),
            "closed": [[o, state, trader] for o, (state, trader) in sorted(self._closed.items())],
        }

    def state_hash(self) -> str:
        """SHA-256 of the canonical book: equal to matching-engine's Q-16 hash at this sequence."""
        text = json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode()).hexdigest()
