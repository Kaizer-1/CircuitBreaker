"""The whole order book of one ticker, rebuilt from stored events for time travel (Q-12, Q-13).

Apply only, never decide (docs/03-architecture/04-event-sourcing-and-cqrs.md, "Decide vs apply"):
the book starts from matching-engine's snapshot (Q-15) and applies the events after it (Q-14).
`OrderAccepted` adds an order, `TradeExecuted` reduces both orders (0 finishes them, FILLED),
`OrderCancelled` finishes what is left (CANCELLED), `OrderExpired` records a tombstone (EXPIRED),
and `OrderCancelRejected` changes nothing. There is no matching code here, so the past comes out
exactly as it was recorded, whatever the engine's rules are today.

Unlike the live depth (domain/depth.py) this keeps every resting order with its trader, time
priority and the finished orders the engine still remembers, because it must reproduce the
engine's `state_hash` (Q-16) by the rule in api/matching-engine.yaml. That rule includes the
engine's memory of one hour (ADR-0017 note "A memory of one hour"): `forget_before` trails the
latest event by an hour and never moves back, and an order that closed before it is forgotten.
It is this service's own code: nothing is imported from matching-engine, and a contract test
checks the hash against the examples the spec states.

Each instance is a private copy built for one request: nothing here is shared with the live
projection, so time travel can never change live state.
"""

import hashlib
import json
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from market_data_service.domain.depth import Level

# The engine's memory of finished orders, in event time. Part of the hash rule: it must equal
# matching-engine's CLOSED_MEMORY_MS (the contract test checks the spec's examples).
CLOSED_MEMORY_MS = 3_600_000

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MS = timedelta(milliseconds=1)


def _ms(timestamp: str) -> int:
    return (datetime.fromisoformat(timestamp) - _EPOCH) // _MS


def _timestamp(ms: int) -> str:
    return (_EPOCH + ms * _MS).strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


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
        # order_id -> (state, trader_id, closed_at, closed_at in ms); by closing time, oldest first
        self._closed: dict[str, tuple[str, str, str, int]] = {}
        self._closing: deque[tuple[int, str]] = deque()
        self._forget_before: int | None = None  # ms; None until the first event

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
        forget_before: str | None = None,
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
        for c in sorted(closed, key=lambda c: (_ms(c["closed_at"]), c["order_id"])):
            book._close(c["order_id"], c["state"], c["trader_id"], c["closed_at"])
        if forget_before is not None:
            book._forget(_ms(forget_before))
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
                    self._finish(order, "FILLED", occurred_at)
        elif event_type == "OrderCancelled":
            order = self._live(data["order_id"])
            if int(data["cancelled_quantity"]) != order.remaining:
                raise CorruptHistoryError(f"cancel of {order.order_id}: quantity does not match")
            self._finish(order, "CANCELLED", occurred_at)
        elif event_type == "OrderExpired":
            self._known_new(data["order_id"])
            self._close(data["order_id"], "EXPIRED", data["trader_id"], occurred_at)
        elif event_type != "OrderCancelRejected":  # a rejection changes nothing
            raise CorruptHistoryError(f"unknown event type {event_type}")
        self.sequence = sequence
        self.as_of = occurred_at
        self._forget(_ms(occurred_at) - CLOSED_MEMORY_MS)

    def _remembered(self, order_id: str) -> bool:
        entry = self._closed.get(order_id)
        return entry is not None and self._kept(entry[3])

    def _kept(self, closed_ms: int) -> bool:
        return self._forget_before is None or closed_ms >= self._forget_before

    def _known_new(self, order_id: str) -> None:
        if order_id in self._orders or self._remembered(order_id):
            raise CorruptHistoryError(f"order {order_id} is already known")

    def _close(self, order_id: str, state: str, trader_id: str, at: str) -> None:
        at_ms = _ms(at)
        self._closed[order_id] = (state, trader_id, at, at_ms)
        self._closing.append((at_ms, order_id))

    def _forget(self, before: int) -> None:
        """Move forget_before up to `before` (never back) and drop what closed before it."""
        if self._forget_before is not None and before <= self._forget_before:
            return
        self._forget_before = before
        while self._closing and self._closing[0][0] < before:
            at_ms, order_id = self._closing.popleft()
            entry = self._closed.get(order_id)
            if entry is not None and entry[3] == at_ms:  # not closed again since (a tombstone)
                del self._closed[order_id]

    def _live(self, order_id: str) -> _Order:
        try:
            return self._orders[order_id]
        except KeyError:
            raise CorruptHistoryError(f"order {order_id} is not in the book") from None

    def _rest(self, order: _Order, price: int) -> None:
        self._orders[order.order_id] = order
        self._levels[order.side].setdefault(price, []).append(order)

    def _finish(self, order: _Order, state: str, at: str) -> None:
        del self._orders[order.order_id]
        self._close(order.order_id, state, order.trader_id, at)
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
            "closed": [
                [o, state, trader, at]
                for o, (state, trader, at, at_ms) in sorted(self._closed.items())
                if self._kept(at_ms)
            ],
            "forget_before": (
                None if self._forget_before is None else _timestamp(self._forget_before)
            ),
        }

    def state_hash(self) -> str:
        """SHA-256 of the canonical book: equal to matching-engine's Q-16 hash at this sequence."""
        text = json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode()).hexdigest()
