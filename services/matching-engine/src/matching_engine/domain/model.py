"""The matching engine's language: commands it receives, events it produces, and book views.

Pure data: no I/O, no framework. Field names follow the event catalog
(docs/03-architecture/03-event-catalog.md); prices are integer paise (ADR-0012).
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

TICKERS = ("CALVERA", "KORVIN", "ASTRYX", "HELIOR", "TAVIRA")
TICK_SIZE_PAISE = 5
MAX_QUANTITY = 10_000
# How long a book remembers a finished or expired order, in event time (ADR-0017 note "A memory of
# one hour"). Part of the state: changing it changes every state_hash, so it is a constant here and
# in market-data-service's replay, never configuration.
CLOSED_MEMORY_MS = 3_600_000

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MS = timedelta(milliseconds=1)


def ms_of(timestamp: str) -> int:
    """Milliseconds since the epoch of an ISO-8601 UTC timestamp such as an event's occurred_at."""
    return (datetime.fromisoformat(timestamp) - _EPOCH) // _MS


def timestamp_of(ms: int) -> str:
    """The event catalog's timestamp form, millisecond precision: 2026-11-20T10:42:01.263Z."""
    moment = _EPOCH + ms * _MS
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class CancelReason(StrEnum):
    USER_REQUESTED = "USER_REQUESTED"
    UNFILLED_MARKET_ORDER = "UNFILLED_MARKET_ORDER"
    SELF_TRADE_PREVENTED = "SELF_TRADE_PREVENTED"
    ORDER_EXPIRED = "ORDER_EXPIRED"


class CancelRejectReason(StrEnum):
    ALREADY_FILLED = "ALREADY_FILLED"
    ALREADY_CANCELLED = "ALREADY_CANCELLED"
    UNKNOWN_ORDER = "UNKNOWN_ORDER"


class OrderState(StrEnum):
    """What the book knows about an order ID. EXPIRED is a tombstone: the ID can never trade."""

    RESTING = "RESTING"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class Outcome(StrEnum):
    PROCESSED = "PROCESSED"  # produced events
    NOTHING_TO_DO = "NOTHING_TO_DO"  # e.g. an expiry for an order that already finished
    REFUSED = "REFUSED"  # e.g. a late OrderPlaced whose ID was expired


class InvalidCommandError(ValueError):
    """A command the engine must never act on (bad price, quantity, ticker...). No events result."""


class CorruptEventError(ValueError):
    """An event that cannot follow the book's current state. Replaying it would corrupt the book."""


# ------------------------------------------------------------------ commands (from orders.v1)


@dataclass(frozen=True)
class Command:
    command_id: str  # the event_id of the incoming message; dedupe key (processed_commands)
    correlation_id: str
    ticker: str
    order_id: str
    trader_id: str


@dataclass(frozen=True)
class PlaceOrder(Command):
    side: Side
    order_type: OrderType
    quantity: int
    limit_price_paise: int | None
    protection_price_paise: int | None
    placed_at: str


@dataclass(frozen=True)
class CancelOrder(Command):
    requested_at: str


@dataclass(frozen=True)
class ExpireOrder(Command):
    requested_at: str


# ------------------------------------------------------------------ events (to matching.v1)


@dataclass(frozen=True)
class MarketEvent:
    sequence: int
    ticker: str
    event_id: str
    occurred_at: str
    causation_id: str | None
    correlation_id: str

    @property
    def event_type(self) -> str:
        return type(self).__name__


@dataclass(frozen=True)
class OrderAccepted(MarketEvent):
    order_id: str
    trader_id: str
    side: Side
    order_type: OrderType
    quantity: int
    limit_price_paise: int | None
    protection_price_paise: int | None


@dataclass(frozen=True)
class TradeExecuted(MarketEvent):
    trade_id: str
    price_paise: int
    quantity: int
    buy_order_id: str
    sell_order_id: str
    buyer_trader_id: str
    seller_trader_id: str
    aggressor_side: Side
    buy_order_remaining_quantity: int
    sell_order_remaining_quantity: int


@dataclass(frozen=True)
class OrderCancelled(MarketEvent):
    order_id: str
    trader_id: str
    side: Side
    cancelled_quantity: int
    reason: CancelReason


@dataclass(frozen=True)
class OrderCancelRejected(MarketEvent):
    order_id: str
    trader_id: str
    reason: CancelRejectReason


@dataclass(frozen=True)
class OrderExpired(MarketEvent):
    order_id: str
    trader_id: str


@dataclass(frozen=True)
class Decision:
    """What the engine decided for one command: the events to store, in order, and why."""

    events: tuple[MarketEvent, ...]
    outcome: Outcome
    detail: str = ""


# ------------------------------------------------------------------ views and snapshots


@dataclass(frozen=True)
class RestingOrder:
    order_id: str
    trader_id: str
    remaining_quantity: int
    accepted_sequence: int  # time priority: lower was first


@dataclass(frozen=True)
class PriceLevel:
    price_paise: int
    orders: tuple[RestingOrder, ...]  # first in, first matched


Level = tuple[int, tuple[tuple[str, str, int, int], ...]]


@dataclass(frozen=True)
class BookSnapshot:
    """The whole state of one ticker's book after `sequence` (local event BookSnapshotTaken).

    `closed` holds the orders finished or expired at or after `forget_before` (one hour before the
    latest event), so cancel rejections and the tombstone rule give the same answers after a
    restart as before it. A placement from before `forget_before` is refused (ADR-0017 note).
    """

    ticker: str
    sequence: int
    as_of: str | None
    bids: tuple[Level, ...]  # high to low
    asks: tuple[Level, ...]  # low to high
    # (order_id, state, trader_id, closed_at), sorted by order_id
    closed: tuple[tuple[str, str, str, str], ...]
    forget_before: str | None = None  # None until the first event

    def to_dict(self) -> dict[str, Any]:
        def levels(side: tuple[Level, ...]) -> list[Any]:
            return [[price, [list(o) for o in orders]] for price, orders in side]

        return {
            "ticker": self.ticker,
            "sequence": self.sequence,
            "as_of": self.as_of,
            "bids": levels(self.bids),
            "asks": levels(self.asks),
            "closed": [list(c) for c in self.closed],
            "forget_before": self.forget_before,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "BookSnapshot":
        def levels(side: list[Any]) -> tuple[Level, ...]:
            return tuple(
                (int(price), tuple((str(o), str(t), int(q), int(s)) for o, t, q, s in orders))
                for price, orders in side
            )

        # Snapshots stored before 2026-11-14 have no closing times: their closed orders count as
        # closed at the snapshot's moment, so they are remembered for one more hour.
        as_of = raw["as_of"]
        closed = tuple(
            (str(c[0]), str(c[1]), str(c[2]), str(c[3] if len(c) > 3 else as_of))
            for c in raw["closed"]
        )
        return cls(
            ticker=str(raw["ticker"]),
            sequence=int(raw["sequence"]),
            as_of=as_of,
            bids=levels(raw["bids"]),
            asks=levels(raw["asks"]),
            closed=closed,
            forget_before=raw.get("forget_before"),
        )
