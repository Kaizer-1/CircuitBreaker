"""The market rules a bot must respect (docs/00-overview.md sections 3 and 4), in integer paise."""

from dataclasses import dataclass

TICK_SIZE_PAISE = 5
MIN_QUANTITY, MAX_QUANTITY = 1, 10_000
PROTECTION = 0.10  # a MARKET order trades at most 10% away from the last price
REFERENCE_PRICES = {
    "CHAIWALA": 24550,
    "JUGAAD": 118000,
    "DABBA": 41275,
    "MONSOON": 8820,
    "FILMY": 53005,
}
TICKERS = tuple(REFERENCE_PRICES)


def floor_tick(price: float) -> int:
    return max(TICK_SIZE_PAISE, int(price // TICK_SIZE_PAISE) * TICK_SIZE_PAISE)


def ceil_tick(price: float) -> int:
    return max(TICK_SIZE_PAISE, -int(-price // TICK_SIZE_PAISE) * TICK_SIZE_PAISE)


def buy_protection(last_price: int) -> int:
    """What portfolio-service reserves per share for a MARKET buy: last + 10%, rounded up."""
    return ceil_tick(last_price * (1 + PROTECTION))


@dataclass(frozen=True)
class Place:
    """An order a strategy wants to place (A-05)."""

    ticker: str
    side: str  # BUY or SELL
    quantity: int
    limit_price_paise: int | None  # None for a MARKET order

    @property
    def order_type(self) -> str:
        return "MARKET" if self.limit_price_paise is None else "LIMIT"


@dataclass(frozen=True)
class Cancel:
    """One of the bot's own resting orders to cancel (A-07)."""

    order_id: str
    ticker: str


Intent = Place | Cancel


@dataclass
class OpenOrder:
    order_id: str
    ticker: str
    side: str
    limit_price_paise: int | None
    remaining: int
    placed_candle: int  # how many candles this bot had seen when it placed it


def valid_shape(order: Place) -> str | None:
    """The rules order-service checks itself (A-05): None if the order is well-formed."""
    if order.ticker not in REFERENCE_PRICES:
        return "UNKNOWN_TICKER"
    if not MIN_QUANTITY <= order.quantity <= MAX_QUANTITY:
        return "VALIDATION_FAILED"
    if order.limit_price_paise is not None and (
        order.limit_price_paise < TICK_SIZE_PAISE or order.limit_price_paise % TICK_SIZE_PAISE
    ):
        return "INVALID_TICK_SIZE"
    return None
