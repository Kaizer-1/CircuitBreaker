"""One order book per ticker: price-time priority matching, event sourced.

Two kinds of method (docs/03-architecture/04-event-sourcing-and-cqrs.md, "Decide vs apply"):

- `decide(command)` holds the business rules and *returns* events. It never changes the book.
- `apply(event)` changes the book according to one event, with no rules of its own. Replay after a
  crash, from a snapshot or for time travel only ever calls `apply`, so history always comes out
  the same even if the matching rules change later.

Matching rules: the best price matches first, then the earliest order at that price; a trade is at
the resting order's price; an incoming order whose next match would be its own trader's resting
order has its remainder cancelled (self-trade prevention, "cancel newest"); a MARKET order never
trades beyond its protection price and its unfilled part is cancelled; LIMIT remainders rest.

Finished orders are remembered for one hour of event time (ADR-0017 note "A memory of one hour"):
`forget_before` trails the latest event by CLOSED_MEMORY_MS and never moves back. An order that
closed before it is forgotten, and a placement made before it is refused, so a late copy of a
forgotten order can still never trade. Memory stays proportional to one hour of orders.
"""

import bisect
import hashlib
import json
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from matching_engine.domain.model import (
    CLOSED_MEMORY_MS,
    MAX_QUANTITY,
    TICK_SIZE_PAISE,
    BookSnapshot,
    CancelOrder,
    CancelReason,
    CancelRejectReason,
    Command,
    CorruptEventError,
    Decision,
    ExpireOrder,
    InvalidCommandError,
    Level,
    MarketEvent,
    OrderAccepted,
    OrderCancelled,
    OrderCancelRejected,
    OrderExpired,
    OrderState,
    OrderType,
    Outcome,
    PlaceOrder,
    PriceLevel,
    RestingOrder,
    Side,
    TradeExecuted,
    ms_of,
    timestamp_of,
)


@dataclass
class _Live:
    """An accepted order with quantity left. A MARKET order is live only while being matched."""

    order_id: str
    trader_id: str
    side: Side
    order_type: OrderType
    price: int | None  # None for MARKET
    remaining: int
    accepted_sequence: int


class _Events:
    """Numbers the events of one decision and fills in their common fields."""

    def __init__(self, book: "OrderBook", command: Command, new_id: Callable[[], str], now: str):
        self._sequence = book.sequence
        self._common = {
            "ticker": book.ticker,
            "occurred_at": now,
            "causation_id": command.command_id,
            "correlation_id": command.correlation_id,
        }
        self._new_id = new_id
        self.made: list[MarketEvent] = []

    def next(self) -> dict[str, Any]:
        self._sequence += 1
        return {"sequence": self._sequence, "event_id": self._new_id(), **self._common}

    def new_id(self) -> str:
        return self._new_id()

    def add(self, event: MarketEvent) -> None:
        self.made.append(event)

    def decision(self, outcome: Outcome = Outcome.PROCESSED) -> Decision:
        return Decision(tuple(self.made), outcome)


def _valid_price(price: int) -> bool:
    return price >= TICK_SIZE_PAISE and price % TICK_SIZE_PAISE == 0


class OrderBook:
    def __init__(self, ticker: str) -> None:
        self.ticker = ticker
        self.sequence = 0  # last applied event
        self.as_of: str | None = None  # occurred_at of the last applied event
        self._live: dict[str, _Live] = {}
        self._levels: dict[Side, dict[int, deque[_Live]]] = {Side.BUY: {}, Side.SELL: {}}
        self._prices: dict[Side, list[int]] = {Side.BUY: [], Side.SELL: []}  # ascending
        # order_id -> (state, trader_id, closed_at, closed_at in ms), and the same orders by closing
        # time, oldest first, so forgetting costs only what is forgotten.
        self._closed: dict[str, tuple[OrderState, str, str, int]] = {}
        self._closing: deque[tuple[int, str]] = deque()
        self._forget_before: int | None = None  # ms; None until the first event

    # ------------------------------------------------------------------ queries

    def _priority_prices(self, side: Side) -> Iterator[int]:
        """Best first: bids high to low, asks low to high."""
        prices = self._prices[side]
        return reversed(prices) if side is Side.BUY else iter(prices)

    def _levels_view(self, side: Side) -> list[PriceLevel]:
        return [
            PriceLevel(
                price,
                tuple(
                    RestingOrder(o.order_id, o.trader_id, o.remaining, o.accepted_sequence)
                    for o in self._levels[side][price]
                ),
            )
            for price in self._priority_prices(side)
        ]

    def bids(self) -> list[PriceLevel]:
        return self._levels_view(Side.BUY)

    def asks(self) -> list[PriceLevel]:
        return self._levels_view(Side.SELL)

    def best_bid(self) -> int | None:
        return self._prices[Side.BUY][-1] if self._prices[Side.BUY] else None

    def best_ask(self) -> int | None:
        return self._prices[Side.SELL][0] if self._prices[Side.SELL] else None

    def resting(self, order_id: str) -> RestingOrder | None:
        o = self._live.get(order_id)
        if o is None:
            return None
        return RestingOrder(o.order_id, o.trader_id, o.remaining, o.accepted_sequence)

    def _remembered(self, order_id: str) -> tuple[OrderState, str] | None:
        """(state, trader_id) of a finished order the book still remembers."""
        entry = self._closed.get(order_id)
        if entry is None or not self._kept(entry[3]):
            return None  # never seen, or closed before forget_before
        return entry[0], entry[1]

    def _kept(self, closed_ms: int) -> bool:
        return self._forget_before is None or closed_ms >= self._forget_before

    def status_of(self, order_id: str) -> OrderState | None:
        if order_id in self._live:
            return OrderState.RESTING
        closed = self._remembered(order_id)
        return closed[0] if closed else None

    @property
    def forget_before(self) -> str | None:
        return None if self._forget_before is None else timestamp_of(self._forget_before)

    def remembered_count(self) -> int:
        """Finished orders held in memory (a metric; bounded by about one hour of orders)."""
        return len(self._closed)

    # ------------------------------------------------------------------ decide (business rules)

    def decide(self, command: Command, *, new_id: Callable[[], str], now: str) -> Decision:
        """The events this command causes, in order. Raises InvalidCommandError; never mutates."""
        if not isinstance(command, PlaceOrder | CancelOrder | ExpireOrder):
            raise InvalidCommandError(f"unknown command {type(command).__name__}")
        if command.ticker != self.ticker:
            raise InvalidCommandError(f"ticker {command.ticker!r} sent to the {self.ticker} book")
        events = _Events(self, command, new_id, now)
        if isinstance(command, PlaceOrder):
            return self._decide_place(command, events)
        if isinstance(command, CancelOrder):
            return self._decide_cancel(command, events)
        return self._decide_expire(command, events)

    def _validate(self, c: PlaceOrder) -> int:
        """Return the price limit of the order (limit price, or protection price for MARKET)."""
        if not 1 <= c.quantity <= MAX_QUANTITY:
            raise InvalidCommandError(f"quantity {c.quantity} is not between 1 and {MAX_QUANTITY}")
        if c.order_type is OrderType.LIMIT:
            price, other, other_name = c.limit_price_paise, c.protection_price_paise, "protection"
            if price is None:
                raise InvalidCommandError("a LIMIT order needs limit_price_paise")
        else:
            price, other, other_name = c.protection_price_paise, c.limit_price_paise, "limit"
            if price is None:
                raise InvalidCommandError("a MARKET order needs protection_price_paise")
        if other is not None:
            raise InvalidCommandError(f"a {c.order_type} order has no {other_name}_price_paise")
        if not _valid_price(price):
            raise InvalidCommandError(f"price {price} is not a multiple of the 5-paise tick")
        return price

    def _decide_place(self, c: PlaceOrder, events: _Events) -> Decision:
        limit = self._validate(c)
        if self._forget_before is not None and ms_of(c.placed_at) < self._forget_before:
            # Older than the book's memory: it may be a late copy of an order already forgotten.
            return Decision((), Outcome.REFUSED, "placed before forget_before: never trades")
        known = self.status_of(c.order_id)
        if known is OrderState.EXPIRED:
            return Decision((), Outcome.REFUSED, "order id was expired (tombstone): never trades")
        if known is not None:
            return Decision((), Outcome.REFUSED, f"order id already known ({known})")

        events.add(
            OrderAccepted(
                **events.next(),
                order_id=c.order_id,
                trader_id=c.trader_id,
                side=c.side,
                order_type=c.order_type,
                quantity=c.quantity,
                limit_price_paise=c.limit_price_paise,
                protection_price_paise=c.protection_price_paise,
            )
        )
        buying = c.side is Side.BUY
        opposite = Side.SELL if buying else Side.BUY
        remaining = c.quantity
        own_order_next = False
        for price in self._priority_prices(opposite):
            if (buying and price > limit) or (not buying and price < limit):
                break  # the book no longer crosses this order's limit or protection price
            for resting in self._levels[opposite][price]:
                if resting.trader_id == c.trader_id:
                    own_order_next = True  # self-trade prevention: stop before trading with self
                    break
                fill = min(remaining, resting.remaining)
                remaining -= fill
                left_resting = resting.remaining - fill
                buy, sell = (c, resting) if buying else (resting, c)
                events.add(
                    TradeExecuted(
                        **events.next(),
                        trade_id=events.new_id(),
                        price_paise=price,
                        quantity=fill,
                        buy_order_id=buy.order_id,
                        sell_order_id=sell.order_id,
                        buyer_trader_id=buy.trader_id,
                        seller_trader_id=sell.trader_id,
                        aggressor_side=c.side,
                        buy_order_remaining_quantity=remaining if buying else left_resting,
                        sell_order_remaining_quantity=left_resting if buying else remaining,
                    )
                )
                if remaining == 0:
                    break
            if remaining == 0 or own_order_next:
                break

        if remaining > 0 and (own_order_next or c.order_type is OrderType.MARKET):
            reason = (
                CancelReason.SELF_TRADE_PREVENTED
                if own_order_next
                else CancelReason.UNFILLED_MARKET_ORDER
            )
            events.add(
                OrderCancelled(
                    **events.next(),
                    order_id=c.order_id,
                    trader_id=c.trader_id,
                    side=c.side,
                    cancelled_quantity=remaining,
                    reason=reason,
                )
            )
        return events.decision()

    def _decide_cancel(self, c: CancelOrder, events: _Events) -> Decision:
        live = self._live.get(c.order_id)
        if live is not None and live.trader_id == c.trader_id:
            events.add(
                OrderCancelled(
                    **events.next(),
                    order_id=c.order_id,
                    trader_id=c.trader_id,
                    side=live.side,
                    cancelled_quantity=live.remaining,
                    reason=CancelReason.USER_REQUESTED,
                )
            )
            return events.decision()
        reason = CancelRejectReason.UNKNOWN_ORDER  # never seen, expired, or not this trader's
        state, owner = self._remembered(c.order_id) or (None, None)
        if live is None and owner == c.trader_id:
            if state is OrderState.FILLED:
                reason = CancelRejectReason.ALREADY_FILLED
            elif state is OrderState.CANCELLED:
                reason = CancelRejectReason.ALREADY_CANCELLED
        events.add(
            OrderCancelRejected(
                **events.next(), order_id=c.order_id, trader_id=c.trader_id, reason=reason
            )
        )
        return events.decision()

    def _decide_expire(self, c: ExpireOrder, events: _Events) -> Decision:
        """ADR-0017: make sure this order can never trade, so its money can be released."""
        live = self._live.get(c.order_id)
        if live is not None:
            if live.trader_id != c.trader_id:
                return Decision((), Outcome.REFUSED, "expiry for another trader's order")
            events.add(
                OrderCancelled(
                    **events.next(),
                    order_id=c.order_id,
                    trader_id=c.trader_id,
                    side=live.side,
                    cancelled_quantity=live.remaining,
                    reason=CancelReason.ORDER_EXPIRED,
                )
            )
            return events.decision()
        if self._remembered(c.order_id) is not None:
            return Decision((), Outcome.NOTHING_TO_DO, "order already finished")
        events.add(OrderExpired(**events.next(), order_id=c.order_id, trader_id=c.trader_id))
        return events.decision()

    # ------------------------------------------------------------------ apply (no rules)

    def apply(self, event: MarketEvent) -> None:
        if event.ticker != self.ticker:
            raise CorruptEventError(f"event for ticker {event.ticker} applied to {self.ticker}")
        if event.sequence != self.sequence + 1:
            raise CorruptEventError(f"sequence {event.sequence} does not follow {self.sequence}")
        if isinstance(event, OrderAccepted):
            self._apply_accepted(event)
        elif isinstance(event, TradeExecuted):
            self._apply_trade(event)
        elif isinstance(event, OrderCancelled):
            self._apply_cancelled(event)
        elif isinstance(event, OrderExpired):
            self._check_new_id(event.order_id)
            self._close(event.order_id, OrderState.EXPIRED, event.trader_id, event.occurred_at)
        elif not isinstance(event, OrderCancelRejected):  # a rejection changes nothing
            raise CorruptEventError(f"unknown event type {event.event_type}")
        self.sequence = event.sequence
        self.as_of = event.occurred_at
        self._forget(ms_of(event.occurred_at) - CLOSED_MEMORY_MS)

    def _check_new_id(self, order_id: str) -> None:
        if order_id in self._live or self._remembered(order_id) is not None:
            raise CorruptEventError(f"order {order_id} is already known")

    def _close(self, order_id: str, state: OrderState, trader_id: str, at: str) -> None:
        at_ms = ms_of(at)
        self._closed[order_id] = (state, trader_id, at, at_ms)
        self._closing.append((at_ms, order_id))

    def _forget(self, before: int) -> None:
        """Move forget_before up to `before` (never back) and drop what closed before it."""
        if self._forget_before is not None and before <= self._forget_before:
            return
        self._forget_before = before
        closing = self._closing
        while closing and closing[0][0] < before:
            at_ms, order_id = closing.popleft()
            entry = self._closed.get(order_id)
            if entry is not None and entry[3] == at_ms:  # not closed again since (a tombstone)
                del self._closed[order_id]

    def _live_order(self, order_id: str) -> _Live:
        try:
            return self._live[order_id]
        except KeyError:
            raise CorruptEventError(f"order {order_id} is not in the book") from None

    def _apply_accepted(self, e: OrderAccepted) -> None:
        self._check_new_id(e.order_id)
        order = _Live(
            e.order_id,
            e.trader_id,
            e.side,
            e.order_type,
            e.limit_price_paise,
            e.quantity,
            e.sequence,
        )
        self._live[e.order_id] = order
        if order.price is not None:
            self._add_to_level(order, order.price)

    def _apply_trade(self, e: TradeExecuted) -> None:
        buy, sell = self._live_order(e.buy_order_id), self._live_order(e.sell_order_id)
        if (buy.remaining - e.quantity, sell.remaining - e.quantity) != (
            e.buy_order_remaining_quantity,
            e.sell_order_remaining_quantity,
        ):
            raise CorruptEventError(f"trade {e.trade_id}: remaining quantities do not add up")
        for order in (buy, sell):
            order.remaining -= e.quantity
            if order.remaining == 0:
                self._finish(order, OrderState.FILLED, e.occurred_at)

    def _apply_cancelled(self, e: OrderCancelled) -> None:
        order = self._live_order(e.order_id)
        if e.cancelled_quantity != order.remaining:
            raise CorruptEventError(
                f"cancelled_quantity {e.cancelled_quantity} != remaining {order.remaining}"
            )
        self._finish(order, OrderState.CANCELLED, e.occurred_at)

    def _add_to_level(self, order: _Live, price: int) -> None:
        levels = self._levels[order.side]
        if price not in levels:
            levels[price] = deque()
            bisect.insort(self._prices[order.side], price)
        levels[price].append(order)

    def _finish(self, order: _Live, state: OrderState, at: str) -> None:
        del self._live[order.order_id]
        self._close(order.order_id, state, order.trader_id, at)
        if order.price is None:
            return  # a MARKET order was never in a price level
        level = self._levels[order.side][order.price]
        level.remove(order)
        if not level:
            del self._levels[order.side][order.price]
            self._prices[order.side].remove(order.price)

    # ------------------------------------------------------------------ snapshots and hash

    def snapshot(self) -> BookSnapshot:
        if any(o.order_type is OrderType.MARKET for o in self._live.values()):
            raise CorruptEventError("a market order is still being matched")

        def side(s: Side) -> tuple[Level, ...]:
            return tuple(
                (
                    lv.price_paise,
                    tuple(
                        (o.order_id, o.trader_id, o.remaining_quantity, o.accepted_sequence)
                        for o in lv.orders
                    ),
                )
                for lv in self._levels_view(s)
            )

        closed = tuple(
            (oid, state.value, trader, at)
            for oid, (state, trader, at, at_ms) in sorted(self._closed.items())
            if self._kept(at_ms)
        )
        return BookSnapshot(
            self.ticker,
            self.sequence,
            self.as_of,
            side(Side.BUY),
            side(Side.SELL),
            closed,
            self.forget_before,
        )

    @classmethod
    def from_snapshot(cls, snapshot: BookSnapshot) -> "OrderBook":
        book = cls(snapshot.ticker)
        book.sequence, book.as_of = snapshot.sequence, snapshot.as_of
        for side, levels in ((Side.BUY, snapshot.bids), (Side.SELL, snapshot.asks)):
            for price, orders in levels:
                for order_id, trader_id, remaining, accepted in orders:
                    order = _Live(
                        order_id, trader_id, side, OrderType.LIMIT, price, remaining, accepted
                    )
                    book._live[order_id] = order
                    book._add_to_level(order, price)
        for order_id, state, trader, at in sorted(
            snapshot.closed, key=lambda c: (ms_of(c[3]), c[0])
        ):
            book._close(order_id, OrderState(state), trader, at)
        if snapshot.forget_before is not None:
            book._forget(ms_of(snapshot.forget_before))  # drops what closed before it, if any
        bid, ask = book.best_bid(), book.best_ask()
        if bid is not None and ask is not None and bid >= ask:
            raise CorruptEventError(f"snapshot has a crossed book: bid {bid} >= ask {ask}")
        return book

    def state_hash(self) -> str:
        """SHA-256 of the canonical snapshot: equal hashes mean identical books."""
        canonical = json.dumps(self.snapshot().to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()
