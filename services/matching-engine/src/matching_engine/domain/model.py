"""The matching engine's language: commands it receives, events it produces, and book views.

Pure data: no I/O, no framework. Field names follow the event catalog
(docs/03-architecture/03-event-catalog.md); prices are integer paise (ADR-0012).
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

TICKERS = ("CALVERA", "KORVIN", "ASTRYX", "HELIOR", "TAVIRA")
TICK_SIZE_PAISE = 5
MAX_QUANTITY = 10_000


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

    `closed` holds every finished or expired order ID, so cancel rejections and the tombstone rule
    give the same answers after a restart as before it.
    """

    ticker: str
    sequence: int
    as_of: str | None
    bids: tuple[Level, ...]  # high to low
    asks: tuple[Level, ...]  # low to high
    closed: tuple[tuple[str, str, str], ...]  # (order_id, state, trader_id), sorted by order_id

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
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "BookSnapshot":
        def levels(side: list[Any]) -> tuple[Level, ...]:
            return tuple(
                (int(price), tuple((str(o), str(t), int(q), int(s)) for o, t, q, s in orders))
                for price, orders in side
            )

        return cls(
            ticker=str(raw["ticker"]),
            sequence=int(raw["sequence"]),
            as_of=raw["as_of"],
            bids=levels(raw["bids"]),
            asks=levels(raw["asks"]),
            closed=tuple((str(o), str(s), str(t)) for o, s, t in raw["closed"]),
        )
