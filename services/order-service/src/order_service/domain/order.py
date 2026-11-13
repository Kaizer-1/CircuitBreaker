"""The Order aggregate: validation (A-05) and the status an order moves through.

Status only moves forward: PENDING -> OPEN -> PARTIALLY_FILLED -> FILLED, or -> CANCELLED, or
REJECTED (failed reservation, or not published within 60 s). The matching engine's events are
the facts: an order rejected for PUBLISH_TIMEOUT that the engine accepted anyway (a publish that
hung past the limit) becomes OPEN, because the engine is the source of truth for the book.
"""

from dataclasses import dataclass
from enum import StrEnum

TICKERS = ("CALVERA", "KORVIN", "ASTRYX", "HELIOR", "TAVIRA")
TICK_SIZE_PAISE = 5
MAX_QUANTITY = 10_000
PUBLISH_TIMEOUT_SECONDS = 60  # ADR-0011: the relay never publishes an older OrderPlaced


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class OrderStatus(StrEnum):
    PENDING = "PENDING"  # accepted by order-service, not yet seen by the engine
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


FINISHED = {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED}


class RejectionReason(StrEnum):
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    INSUFFICIENT_SHARES = "INSUFFICIENT_SHARES"
    INVALID_TICK_SIZE = "INVALID_TICK_SIZE"
    UNKNOWN_TICKER = "UNKNOWN_TICKER"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    PUBLISH_TIMEOUT = "PUBLISH_TIMEOUT"


class OrderValidationError(ValueError):
    def __init__(self, code: RejectionReason, field: str, message: str) -> None:
        super().__init__(message)
        self.code, self.field, self.message = code, field, message


@dataclass(frozen=True)
class OrderRequest:
    """POST /v1/orders, as parsed: still unvalidated (the codes need the specific rule)."""

    ticker: str
    side: Side
    order_type: OrderType
    quantity: int
    limit_price_paise: int | None


def validate(request: OrderRequest) -> None:
    """Ticker, quantity, LIMIT has a price and MARKET has none, price on the 5-paise tick."""
    if request.ticker not in TICKERS:
        raise OrderValidationError(
            RejectionReason.UNKNOWN_TICKER,
            "ticker",
            f"{request.ticker} is not one of {', '.join(TICKERS)}",
        )
    if not 1 <= request.quantity <= MAX_QUANTITY:
        raise OrderValidationError(
            RejectionReason.VALIDATION_FAILED, "quantity", f"must be between 1 and {MAX_QUANTITY}"
        )
    price = request.limit_price_paise
    if request.order_type is OrderType.MARKET:
        if price is not None:
            raise OrderValidationError(
                RejectionReason.VALIDATION_FAILED,
                "limit_price_paise",
                "a MARKET order has no limit price",
            )
        return
    if price is None:
        raise OrderValidationError(
            RejectionReason.VALIDATION_FAILED,
            "limit_price_paise",
            "a LIMIT order needs a limit price",
        )
    if price < TICK_SIZE_PAISE or price % TICK_SIZE_PAISE:
        raise OrderValidationError(
            RejectionReason.INVALID_TICK_SIZE,
            "limit_price_paise",
            f"{price} is not a multiple of 5",
        )


def round_half_up(numerator: int, denominator: int) -> int:
    return (2 * numerator + denominator) // (2 * denominator)


@dataclass
class Order:
    order_id: str
    trader_id: str
    ticker: str
    side: Side
    order_type: OrderType
    quantity: int
    limit_price_paise: int | None
    protection_price_paise: int | None
    reservation_id: str | None
    status: OrderStatus
    placed_at: str
    updated_at: str
    filled_quantity: int = 0
    filled_value_paise: int = 0  # sum of quantity x price over fills (for the average price)
    cancel_requested: bool = False
    cancel_reason: str | None = None
    rejection_reason: RejectionReason | None = None
    last_sequence: int = 0  # the last matching.v1 sequence applied to this order
    published: bool = False  # its OrderPlaced reached orders.v1 (a cancel must never overtake it)

    @property
    def remaining_quantity(self) -> int:
        if self.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
            return 0
        return self.quantity - self.filled_quantity

    @property
    def average_fill_price_paise(self) -> int | None:
        if not self.filled_quantity:
            return None
        return round_half_up(self.filled_value_paise, self.filled_quantity)

    def cancellable(self) -> bool:
        return self.status not in FINISHED and not self.cancel_requested

    # ---------------------------------------------------------- what matching.v1 events do

    def _fresh(self, sequence: int, now: str) -> bool:
        """Events of one order arrive in sequence order; anything not newer is a replay."""
        if sequence <= self.last_sequence:
            return False
        self.last_sequence, self.updated_at = sequence, now
        return True

    def accepted(self, sequence: int, now: str) -> None:
        if not self._fresh(sequence, now):
            return
        if (
            self.status is OrderStatus.PENDING
            or self.rejection_reason is RejectionReason.PUBLISH_TIMEOUT
        ):
            self.status, self.rejection_reason = OrderStatus.OPEN, None

    def traded(
        self, sequence: int, now: str, quantity: int, price_paise: int, remaining: int
    ) -> None:
        if not self._fresh(sequence, now):
            return
        self.filled_quantity += quantity
        self.filled_value_paise += quantity * price_paise
        self.rejection_reason = None
        self.status = OrderStatus.FILLED if remaining == 0 else OrderStatus.PARTIALLY_FILLED
        if remaining == 0:
            self.cancel_requested = False

    def cancelled(self, sequence: int, now: str, reason: str) -> None:
        if not self._fresh(sequence, now):
            return
        self.status, self.cancel_reason, self.cancel_requested = (
            OrderStatus.CANCELLED,
            reason,
            False,
        )

    def cancel_rejected(self, sequence: int, now: str) -> None:
        if self._fresh(sequence, now):
            self.cancel_requested = False

    def expired(self, sequence: int, now: str) -> None:
        """The engine never saw this order and now never will (ADR-0017)."""
        if self._fresh(sequence, now) and self.status is OrderStatus.PENDING:
            self.status, self.rejection_reason = (
                OrderStatus.REJECTED,
                RejectionReason.PUBLISH_TIMEOUT,
            )

    def reject(self, reason: RejectionReason, now: str) -> None:
        self.status, self.rejection_reason, self.updated_at = OrderStatus.REJECTED, reason, now
