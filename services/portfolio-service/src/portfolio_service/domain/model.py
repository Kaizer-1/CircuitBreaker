"""The Account aggregate: accounts, holdings, reservations (data ownership doc, database portfolio).

Money is integer paise (ADR-0012). The invariants, also enforced by CHECK constraints in Postgres:
cash >= reserved cash >= 0, and per ticker quantity >= reserved quantity >= 0.
"""

from dataclasses import dataclass
from enum import StrEnum

TICKERS = ("CHAIWALA", "JUGAAD", "DABBA", "MONSOON", "FILMY")
# The starting prices used before any trade (docs/00-overview.md section 3).
REFERENCE_PRICES = {
    "CHAIWALA": 24550,
    "JUGAAD": 118000,
    "DABBA": 41275,
    "MONSOON": 8820,
    "FILMY": 53005,
}
TICK_SIZE_PAISE = 5
HUMAN_STARTING_CASH = 10_000_000  # ₹1,00,000
BOT_STARTING_CASH = 50_000_000  # ₹5,00,000
BOT_STARTING_SHARES = 1_000  # of each ticker: bots are the first sellers (no short selling)
LOAD_STARTING_CASH = 10_000_000


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class AccountKind(StrEnum):
    HUMAN = "HUMAN"
    BOT = "BOT"
    LOAD = "LOAD"


class ReservationStatus(StrEnum):
    ACTIVE = "ACTIVE"  # holds cash or shares for an order that can still trade
    CONSUMED = "CONSUMED"  # the order filled; any leftover cash was released
    RELEASED = "RELEASED"  # the order was cancelled or expired; what was left was released


class InsufficientFundsError(Exception):
    def __init__(self, needed: int, available: int) -> None:
        super().__init__(f"needs {needed} paise, available {available} paise")
        self.needed, self.available = needed, available


class InsufficientSharesError(Exception):
    def __init__(self, ticker: str, needed: int, available: int) -> None:
        super().__init__(f"sells {needed} {ticker} shares, available {available}")
        self.ticker, self.needed, self.available = ticker, needed, available


class NicknameTakenError(Exception):
    pass


class AccountNotFoundError(Exception):
    pass


class InvalidReservationError(ValueError):
    pass


class SettlementError(Exception):
    """A matching event that cannot be applied without breaking an invariant. Never expected: it
    would mean a trade on money that was not reserved, which ADR-0017 rules out."""


@dataclass
class Account:
    trader_id: str
    nickname: str
    kind: AccountKind
    cash_paise: int
    reserved_cash_paise: int
    starting_value_paise: int
    created_at: str

    @property
    def available_cash_paise(self) -> int:
        return self.cash_paise - self.reserved_cash_paise


@dataclass
class Holding:
    trader_id: str
    ticker: str
    quantity: int = 0
    reserved_quantity: int = 0
    avg_cost_paise: int = 0

    @property
    def available_quantity(self) -> int:
        return self.quantity - self.reserved_quantity


@dataclass(frozen=True)
class ReservationRequest:
    order_id: str
    trader_id: str
    ticker: str
    side: Side
    order_type: OrderType
    quantity: int
    limit_price_paise: int | None


@dataclass
class Reservation:
    reservation_id: str
    order_id: str
    trader_id: str
    ticker: str
    side: Side
    order_type: OrderType
    quantity: int
    limit_price_paise: int | None
    protection_price_paise: int | None
    reserved_cash_paise: int  # as reserved; never changes
    reserved_quantity: int  # as reserved; never changes
    remaining_cash_paise: int  # still held for this order
    remaining_quantity: int  # still held for this order (SELL)
    status: ReservationStatus
    confirmed: bool  # the engine accepted the order (OrderAccepted seen)
    expiry_requested_at: str | None
    created_at: str
