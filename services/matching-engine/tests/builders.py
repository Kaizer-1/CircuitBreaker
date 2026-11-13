"""Small builders so tests read like the rules they check (no I/O, deterministic IDs and times)."""

from collections.abc import Callable, Iterator
from itertools import count

from matching_engine.domain.book import OrderBook
from matching_engine.domain.model import (
    CancelOrder,
    Command,
    Decision,
    ExpireOrder,
    MarketEvent,
    OrderType,
    PlaceOrder,
    Side,
)

T0 = "2026-11-20T10:42:01.250Z"
TICKER = "CALVERA"

# Traders and orders get readable but valid UUIDs: trader 1 is ...-000000000001.
ALICE = "a11ce000-0000-4000-8000-000000000001"
BOB = "b0b00000-0000-4000-8000-000000000002"
CARA = "ca7a0000-0000-4000-8000-000000000003"
DEV = "de700000-0000-4000-8000-000000000004"


def oid(n: int) -> str:
    return f"0dde0000-0000-4000-8000-{n:012d}"


def id_source(prefix: str = "e0e0e0e0") -> Callable[[], str]:
    counter: Iterator[int] = count(1)
    return lambda: f"{prefix}-0000-4000-8000-{next(counter):012d}"


_commands: Iterator[int] = count(1)


def _cid() -> str:
    return f"c0c0c0c0-0000-4000-8000-{next(_commands):012d}"


def limit(
    n: int,
    trader: str,
    side: Side | str,
    quantity: int,
    price: int,
    *,
    ticker: str = TICKER,
) -> PlaceOrder:
    return PlaceOrder(
        command_id=_cid(),
        correlation_id=f"req-{n}",
        ticker=ticker,
        order_id=oid(n),
        trader_id=trader,
        side=Side(side),
        order_type=OrderType.LIMIT,
        quantity=quantity,
        limit_price_paise=price,
        protection_price_paise=None,
        placed_at=T0,
    )


def market(
    n: int,
    trader: str,
    side: Side | str,
    quantity: int,
    protection: int,
    *,
    ticker: str = TICKER,
) -> PlaceOrder:
    return PlaceOrder(
        command_id=_cid(),
        correlation_id=f"req-{n}",
        ticker=ticker,
        order_id=oid(n),
        trader_id=trader,
        side=Side(side),
        order_type=OrderType.MARKET,
        quantity=quantity,
        limit_price_paise=None,
        protection_price_paise=protection,
        placed_at=T0,
    )


def cancel(n: int, trader: str, *, ticker: str = TICKER) -> CancelOrder:
    return CancelOrder(
        command_id=_cid(),
        correlation_id=f"cancel-{n}",
        ticker=ticker,
        order_id=oid(n),
        trader_id=trader,
        requested_at=T0,
    )


def expire(n: int, trader: str, *, ticker: str = TICKER) -> ExpireOrder:
    return ExpireOrder(
        command_id=_cid(),
        correlation_id=f"expiry-{n}",
        ticker=ticker,
        order_id=oid(n),
        trader_id=trader,
        requested_at=T0,
    )


class Market:
    """One order book plus the decide-then-apply loop the application runs for every command."""

    def __init__(self, ticker: str = TICKER, *, id_prefix: str = "e0e0e0e0") -> None:
        self.book = OrderBook(ticker)
        self.new_id = id_source(id_prefix)
        self.log: list[MarketEvent] = []

    def submit(self, command: Command) -> Decision:
        decision = self.book.decide(command, new_id=self.new_id, now=T0)
        for event in decision.events:
            self.book.apply(event)
        self.log.extend(decision.events)
        return decision

    def submit_all(self, *commands: Command) -> list[MarketEvent]:
        events: list[MarketEvent] = []
        for command in commands:
            events.extend(self.submit(command).events)
        return events
