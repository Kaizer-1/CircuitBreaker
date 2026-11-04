"""In-memory stand-ins for the ports, and a small reference exchange that produces matching.v1
event streams like the real engine (price-time priority, trade at the resting price, MARKET
orders that never rest, self-trade prevention by cancelling the incoming order)."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

from market_data_service.domain.candles import Candle
from market_data_service.domain.market import Trade
from market_data_service.ports.event_store import BookSnapshot
from market_data_service.ports.market_store import Changes, StoredMarket
from market_data_service.runtime.events import EventEnvelope, build_event, format_timestamp

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

    def series(self, ticker: str, interval: str) -> list[Candle]:
        return sorted(
            (c for (t, i, _), c in self.candle_rows.items() if t == ticker and i == interval),
            key=lambda c: c.bucket_start_ms,
        )


class FakeEventStore:
    """matching-engine's Q-14/Q-15: every event the exchange produced, plus snapshots."""

    def __init__(self) -> None:
        self.events: dict[str, list[EventEnvelope]] = {}
        self.snapshots: dict[str, BookSnapshot] = {}
        self.down = False
        self.reads = 0

    async def snapshot(self, ticker: str, at_ms: int) -> BookSnapshot:
        if self.down:
            raise ConnectionError("matching-engine is down")
        return self.snapshots.get(ticker, BookSnapshot(0, [], []))

    async def events_after(self, ticker: str, after_sequence: int) -> AsyncIterator[EventEnvelope]:
        if self.down:
            raise ConnectionError("matching-engine is down")
        self.reads += 1
        for event in list(self.events.get(ticker, [])):
            if int(event.data["sequence"]) > after_sequence:
                yield event


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

    def candles_closed(self, ticker: str = "CHAIWALA") -> list[dict[str, Any]]:
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


@dataclass
class Exchange:
    """One ticker's book, deciding like the engine and emitting the events it would publish."""

    ticker: str = "CHAIWALA"
    clock: Clock = field(default_factory=Clock)
    bids: list[_Resting] = field(default_factory=list)  # best first, then time
    asks: list[_Resting] = field(default_factory=list)
    log: list[EventEnvelope] = field(default_factory=list)
    _seq: Any = field(default_factory=lambda: count(1))
    _ids: Any = field(default_factory=lambda: count(1))

    def _event(self, event_type: str, data: dict[str, Any], command: str) -> EventEnvelope:
        at = format_timestamp(self.clock())
        stamp = {
            "OrderAccepted": "accepted_at",
            "TradeExecuted": "executed_at",
            "OrderCancelled": "cancelled_at",
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
        book = self.asks if side == "BUY" else self.bids
        remaining = quantity
        while remaining and book:
            best = book[0]
            if price is not None and (best.price > price if side == "BUY" else best.price < price):
                break
            if best.trader == trader:
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
                book.pop(0)
        if remaining and price is None:
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
        elif remaining:
            own = self.bids if side == "BUY" else self.asks
            own.append(_Resting(order_id, trader, side, price, remaining))  # type: ignore[arg-type]
            own.sort(key=lambda o: -o.price if side == "BUY" else o.price)  # stable: time kept
        return self.log[start:]

    def cancel_oldest(self, side: str) -> list[EventEnvelope]:
        book = self.bids if side == "BUY" else self.asks
        if not book:
            return []
        order = book.pop(0)
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
        def levels(book: list[_Resting]) -> list[dict[str, Any]]:
            out: dict[int, list[dict[str, Any]]] = {}
            for o in book:
                out.setdefault(o.price, []).append(
                    {"order_id": o.order_id, "remaining_quantity": o.remaining}
                )
            return [{"price_paise": p, "orders": orders} for p, orders in out.items()]

        sequence = int(self.log[-1].data["sequence"]) if self.log else 0
        return BookSnapshot(sequence, levels(self.bids), levels(self.asks))

    def advance(self, seconds: float) -> None:
        self.clock.now += timedelta(seconds=seconds)
