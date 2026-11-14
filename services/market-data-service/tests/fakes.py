"""In-memory stand-ins for the ports, and a small reference exchange that produces matching.v1
event streams like the real engine (price-time priority, trade at the resting price, MARKET
orders that never rest, self-trade prevention by cancelling the incoming order, expiry
tombstones and cancel rejections). The exchange also keeps the engine's whole state (finished
orders included) and hashes it by the rule in api/matching-engine.yaml, written here
independently of the service's ReplayBook, so time-travel tests compare two implementations."""

import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

from market_data_service.domain.candles import Candle
from market_data_service.domain.market import Trade
from market_data_service.ports.event_store import BookSnapshot
from market_data_service.ports.market_store import Changes, StoredMarket
from market_data_service.runtime.events import (
    EventEnvelope,
    build_event,
    format_timestamp,
    parse_timestamp,
)

T0 = datetime(2026, 11, 20, 10, 42, 0, tzinfo=UTC)


class Clock:
    def __init__(self, at: datetime = T0) -> None:
        self.now = at

    def __call__(self) -> datetime:
        return self.now


class InMemoryMarketStore:
    def __init__(self) -> None:
        self.rows: dict[str, Changes] = {}
        self.candle_rows: dict[tuple[str, str, int], Candle] = {}
        self.trade_rows: dict[str, list[Trade]] = {}
        self.saves = 0
        self.down = False

    async def migrate(self) -> None:
        return None

    async def load(self, ticker: str) -> StoredMarket | None:
        row = self.rows.get(ticker)
        if row is None:
            return None
        candles = {
            interval: sorted(
                (c for (t, i, _), c in self.candle_rows.items() if t == ticker and i == interval),
                key=lambda c: c.bucket_start_ms,
            )
            for interval in ("5s", "1m", "5m")
        }
        return StoredMarket(
            sequence=row.sequence,
            as_of_ms=row.as_of_ms,
            last_price_paise=row.last_price_paise,
            last_trade_at_ms=row.last_trade_at_ms,
            day=row.day,
            candles=candles,
            recent_trades=await self.trades(ticker, 200),
        )

    async def save(self, changes: Changes) -> None:
        if self.down:
            raise ConnectionError("cassandra is down")
        self.saves += 1
        self.rows[changes.ticker] = changes
        for (interval, bucket), candle in changes.candles.items():
            self.candle_rows[(changes.ticker, interval, bucket)] = candle
        self.trade_rows.setdefault(changes.ticker, []).extend(changes.trades)

    async def candles(
        self, ticker: str, interval: str, from_ms: int, to_ms: int, limit: int
    ) -> list[Candle]:
        found = sorted(
            (
                c
                for (t, i, b), c in self.candle_rows.items()
                if t == ticker and i == interval and from_ms <= b <= to_ms
            ),
            key=lambda c: c.bucket_start_ms,
        )
        return found[-limit:]

    async def trades(self, ticker: str, limit: int) -> list[Trade]:
        return sorted(self.trade_rows.get(ticker, []), key=lambda t: -t.sequence)[:limit]

    async def trades_until(self, ticker: str, until_ms: int, limit: int) -> list[Trade]:
        found = [t for t in self.trade_rows.get(ticker, []) if t.executed_at_ms <= until_ms]
        return sorted(found, key=lambda t: -t.sequence)[:limit]

    async def trades_between(self, ticker: str, from_ms: int, to_ms: int) -> list[Trade]:
        found = [t for t in self.trade_rows.get(ticker, []) if from_ms <= t.executed_at_ms <= to_ms]
        return sorted(found, key=lambda t: t.sequence)

    def series(self, ticker: str, interval: str) -> list[Candle]:
        return sorted(
            (c for (t, i, _), c in self.candle_rows.items() if t == ticker and i == interval),
            key=lambda c: c.bucket_start_ms,
        )


class FakeEventStore:
    """matching-engine's Q-14/Q-15: every event the exchange produced, plus snapshots.

    `snapshots` holds one snapshot per ticker (always returned, like a recent one); `history`
    holds several, and Q-15's rule picks the newest taken at or before the time asked.
    """

    def __init__(self) -> None:
        self.events: dict[str, list[EventEnvelope]] = {}
        self.snapshots: dict[str, BookSnapshot] = {}
        self.history: dict[str, list[BookSnapshot]] = {}
        self.down = False
        self.reads = 0

    async def snapshot(self, ticker: str, at_ms: int) -> BookSnapshot:
        if self.down:
            raise ConnectionError("matching-engine is down")
        taken = [
            s for s in self.history.get(ticker, []) if s.as_of is not None and ms(s.as_of) <= at_ms
        ]
        if taken:
            return max(taken, key=lambda s: s.sequence)
        return self.snapshots.get(ticker, BookSnapshot(0, [], [], as_of=None))

    async def events_after(
        self, ticker: str, after_sequence: int, *, until_ms: int | None = None
    ) -> AsyncIterator[EventEnvelope]:
        if self.down:
            raise ConnectionError("matching-engine is down")
        self.reads += 1
        for event in list(self.events.get(ticker, [])):
            if until_ms is not None and ms(event.occurred_at) > until_ms:
                return
            if int(event.data["sequence"]) > after_sequence:
                yield event


def ms(stamp: str) -> int:
    return int(parse_timestamp(stamp).timestamp() * 1000)


class RecordingPublisher:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, EventEnvelope]] = []
        self.raw: list[tuple[str, bytes]] = []
        self.down = False

    async def publish(self, topic: str, key: str, event: EventEnvelope) -> None:
        if self.down:
            raise ConnectionError("redpanda is down")
        self.sent.append((topic, key, event))

    async def publish_raw(self, topic: str, key: bytes | None, value: bytes) -> None:
        self.raw.append((topic, value))

    def candles_closed(self, ticker: str = "CALVERA") -> list[dict[str, Any]]:
        return [
            e.data
            for _, _, e in self.sent
            if e.event_type == "CandleClosed" and e.data["ticker"] == ticker
        ]


# ---------------------------------------------------------------------- the reference exchange


@dataclass
class _Resting:
    order_id: str
    trader: str
    side: str
    price: int
    remaining: int
    accepted_sequence: int = 0


@dataclass
class Exchange:
    """One ticker's book, deciding like the engine and emitting the events it would publish."""

    ticker: str = "CALVERA"
    clock: Clock = field(default_factory=Clock)
    bids: list[_Resting] = field(default_factory=list)  # best first, then time
    asks: list[_Resting] = field(default_factory=list)
    log: list[EventEnvelope] = field(default_factory=list)
    # order_id -> (state, trader, closed_at). Everything is kept here; what the engine has forgotten
    # is filtered out when the book is read (a different way from ReplayBook's, on purpose).
    closed: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    _seq: Any = field(default_factory=lambda: count(1))
    _ids: Any = field(default_factory=lambda: count(1))

    def _event(self, event_type: str, data: dict[str, Any], command: str) -> EventEnvelope:
        at = format_timestamp(self.clock())
        stamp = {
            "OrderAccepted": "accepted_at",
            "TradeExecuted": "executed_at",
            "OrderCancelled": "cancelled_at",
            "OrderCancelRejected": "rejected_at",
            "OrderExpired": "expired_at",
        }[event_type]
        event = build_event(
            event_type,
            {"sequence": next(self._seq), "ticker": self.ticker, **data, stamp: at},
            producer="matching-engine",
            environment="dev",
            correlation_id=command,
            causation_id=command,
            occurred_at=self.clock(),
        )
        self.log.append(event)
        return event

    def _close(self, order_id: str, state: str, trader: str) -> None:
        self.closed[order_id] = (state, trader, format_timestamp(self.clock()))

    @property
    def forget_before(self) -> str | None:
        """One hour before the newest event (the clock here never runs back)."""
        if not self.log:
            return None
        newest = max(parse_timestamp(e.occurred_at) for e in self.log)
        return format_timestamp(newest - timedelta(hours=1))

    def remembered(self) -> list[tuple[str, str, str, str]]:
        edge = self.forget_before
        return [
            (o, state, t, at)
            for o, (state, t, at) in sorted(self.closed.items())
            if edge is None or parse_timestamp(at) >= parse_timestamp(edge)
        ]

    def _id(self) -> str:
        return f"0dde0000-0000-4000-8000-{next(self._ids):012d}"

    def place(
        self, trader: str, side: str, quantity: int, price: int | None
    ) -> list[EventEnvelope]:
        """One OrderPlaced command; returns the events of this command."""
        order_id, command = self._id(), self._id()
        start = len(self.log)
        self._event(
            "OrderAccepted",
            {
                "order_id": order_id,
                "trader_id": trader,
                "side": side,
                "order_type": "LIMIT" if price is not None else "MARKET",
                "quantity": quantity,
                "limit_price_paise": price,
                "protection_price_paise": None if price is not None else 999_999_995,
            },
            command,
        )
        accepted = int(self.log[-1].data["sequence"])
        book = self.asks if side == "BUY" else self.bids
        remaining = quantity
        while remaining and book:
            best = book[0]
            if price is not None and (best.price > price if side == "BUY" else best.price < price):
                break
            if best.trader == trader:
                self._close(order_id, "CANCELLED", trader)
                self._event(
                    "OrderCancelled",
                    {
                        "order_id": order_id,
                        "trader_id": trader,
                        "side": side,
                        "cancelled_quantity": remaining,
                        "reason": "SELF_TRADE_PREVENTED",
                    },
                    command,
                )
                return self.log[start:]
            fill = min(remaining, best.remaining)
            remaining -= fill
            best.remaining -= fill
            buy, sell = (order_id, best.order_id) if side == "BUY" else (best.order_id, order_id)
            buyer, seller = (trader, best.trader) if side == "BUY" else (best.trader, trader)
            self._event(
                "TradeExecuted",
                {
                    "trade_id": self._id(),
                    "price_paise": best.price,
                    "quantity": fill,
                    "buy_order_id": buy,
                    "sell_order_id": sell,
                    "buyer_trader_id": buyer,
                    "seller_trader_id": seller,
                    "aggressor_side": side,
                    "buy_order_remaining_quantity": remaining if side == "BUY" else best.remaining,
                    "sell_order_remaining_quantity": best.remaining if side == "BUY" else remaining,
                },
                command,
            )
            if best.remaining == 0:
                self._close(best.order_id, "FILLED", best.trader)
                book.pop(0)
        if remaining == 0:
            self._close(order_id, "FILLED", trader)
        elif price is None:
            self._close(order_id, "CANCELLED", trader)
            self._event(
                "OrderCancelled",
                {
                    "order_id": order_id,
                    "trader_id": trader,
                    "side": side,
                    "cancelled_quantity": remaining,
                    "reason": "UNFILLED_MARKET_ORDER",
                },
                command,
            )
        if remaining and price is not None:
            own = self.bids if side == "BUY" else self.asks
            own.append(_Resting(order_id, trader, side, price, remaining, accepted))
            own.sort(key=lambda o: -o.price if side == "BUY" else o.price)  # stable: time kept
        return self.log[start:]

    def cancel_oldest(self, side: str) -> list[EventEnvelope]:
        book = self.bids if side == "BUY" else self.asks
        if not book:
            return []
        order = book.pop(0)
        self._close(order.order_id, "CANCELLED", order.trader)
        start = len(self.log)
        self._event(
            "OrderCancelled",
            {
                "order_id": order.order_id,
                "trader_id": order.trader,
                "side": side,
                "cancelled_quantity": order.remaining,
                "reason": "USER_REQUESTED",
            },
            self._id(),
        )
        return self.log[start:]

    def expire_unseen(self, trader: str) -> list[EventEnvelope]:
        """An expiry for an order the engine never saw: an OrderExpired tombstone (ADR-0017)."""
        order_id = self._id()
        self._close(order_id, "EXPIRED", trader)
        start = len(self.log)
        self._event("OrderExpired", {"order_id": order_id, "trader_id": trader}, self._id())
        return self.log[start:]

    def reject_cancel(self, trader: str) -> list[EventEnvelope]:
        """A cancel for an unknown order: OrderCancelRejected, which changes nothing."""
        start = len(self.log)
        self._event(
            "OrderCancelRejected",
            {"order_id": self._id(), "trader_id": trader, "reason": "UNKNOWN_ORDER"},
            self._id(),
        )
        return self.log[start:]

    def depth(self) -> tuple[list[tuple[int, int, int]], list[tuple[int, int, int]]]:
        """(price, quantity, order count) per level: what market-data must show."""

        def levels(book: list[_Resting]) -> list[tuple[int, int, int]]:
            out: dict[int, list[int]] = {}
            for o in book:
                level = out.setdefault(o.price, [0, 0])
                level[0] += o.remaining
                level[1] += 1
            return [(p, q, n) for p, (q, n) in out.items()]

        return levels(self.bids), levels(self.asks)

    def snapshot(self) -> BookSnapshot:
        """The whole book as matching-engine's Q-15 would answer now (BookState)."""

        def levels(book: list[_Resting]) -> list[dict[str, Any]]:
            out: dict[int, list[dict[str, Any]]] = {}
            for o in book:
                out.setdefault(o.price, []).append(
                    {
                        "order_id": o.order_id,
                        "trader_id": o.trader,
                        "remaining_quantity": o.remaining,
                        "accepted_sequence": o.accepted_sequence,
                    }
                )
            return [{"price_paise": p, "orders": orders} for p, orders in out.items()]

        return BookSnapshot(
            self.sequence,
            levels(self.bids),
            levels(self.asks),
            as_of=self.as_of,
            closed=[
                {"order_id": o, "state": state, "trader_id": t, "closed_at": at}
                for o, state, t, at in self.remembered()
            ],
            forget_before=self.forget_before,
        )

    @property
    def sequence(self) -> int:
        return int(self.log[-1].data["sequence"]) if self.log else 0

    @property
    def as_of(self) -> str | None:
        return self.log[-1].occurred_at if self.log else None

    def state_hash(self) -> str:
        """The engine's Q-16 hash of this book (api/matching-engine.yaml, BookState.state_hash)."""

        def levels(book: list[_Resting]) -> list[Any]:
            out: dict[int, list[Any]] = {}  # the book is best price first, then time
            for o in book:
                out.setdefault(o.price, []).append(
                    [o.order_id, o.trader, o.remaining, o.accepted_sequence]
                )
            return [[price, orders] for price, orders in out.items()]

        canonical = {
            "as_of": self.as_of,
            "asks": levels(self.asks),
            "bids": levels(self.bids),
            "closed": [list(c) for c in self.remembered()],
            "forget_before": self.forget_before,
            "sequence": self.sequence,
            "ticker": self.ticker,
        }
        text = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode()).hexdigest()

    def advance(self, seconds: float) -> None:
        self.clock.now += timedelta(seconds=seconds)
