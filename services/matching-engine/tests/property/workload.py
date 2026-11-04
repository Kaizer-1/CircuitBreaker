"""Random workloads for the property tests, and a reference ledger of cash and shares.

The ledger is a small model of portfolio-service's rules from the docs: reserve before an order is
placed (cash for BUY at its limit or protection price, shares for SELL), settle each trade at the
trade price from the reservations, and release what is left when the order finishes (filled,
cancelled or expired). Orders the ledger cannot afford are rejected, as A-06 would. The engine is
correct only if, fed this way, no balance can go wrong.
"""

from collections import defaultdict
from dataclasses import dataclass, field

from hypothesis import strategies as st

from matching_engine.domain.model import (
    CancelOrder,
    Command,
    ExpireOrder,
    MarketEvent,
    OrderCancelled,
    OrderExpired,
    OrderType,
    PlaceOrder,
    Side,
    TradeExecuted,
)
from tests.builders import ALICE, BOB, CARA, DEV, T0, oid

TRADERS = (ALICE, BOB, CARA, DEV)
TICKERS = ("CHAIWALA", "JUGAAD")
REFERENCE = {"CHAIWALA": 24550, "JUGAAD": 118000}
START_CASH = 5_000_000  # paise each: enough for most orders, not all (some get rejected)
START_SHARES = 300  # of each ticker each: sellers need shares (no short selling)
TICK = 5


@dataclass(frozen=True)
class Place:
    trader: int
    ticker: int
    side: str
    is_market: bool
    quantity: int
    ticks_from_reference: int


@dataclass(frozen=True)
class Cancel:
    ref: int
    by_owner: bool


@dataclass(frozen=True)
class Expire:
    ref: int
    never_placed: bool  # True: an ID nobody placed (the engine writes a tombstone)


@dataclass(frozen=True)
class LatePlace:
    """A delayed copy of an order whose ID was already expired (a zombie relay)."""

    ref: int


Action = Place | Cancel | Expire | LatePlace

_place = st.builds(
    Place,
    trader=st.integers(0, len(TRADERS) - 1),
    ticker=st.integers(0, len(TICKERS) - 1),
    side=st.sampled_from(["BUY", "SELL"]),
    is_market=st.sampled_from([False, False, True]),  # about a third are MARKET orders
    quantity=st.integers(1, 80) | st.just(10_000),
    ticks_from_reference=st.integers(-8, 8),
)
_other = st.one_of(
    st.builds(
        Cancel, ref=st.integers(0, 1000), by_owner=st.sampled_from([True, True, True, False])
    ),
    st.builds(Expire, ref=st.integers(0, 1000), never_placed=st.booleans()),
    st.builds(LatePlace, ref=st.integers(0, 1000)),
)
actions = st.lists(st.one_of(_place, _place, _place, _place, _other), min_size=1, max_size=60)


def protection_price(last_price: int, side: Side) -> int:
    """Last price ± 10%, rounded up (BUY) or down (SELL) to the tick, as portfolio-service does."""
    if side is Side.BUY:
        return -(-(last_price * 110) // (100 * TICK)) * TICK  # ceiling division
    return max(last_price * 90 // (100 * TICK) * TICK, TICK)


@dataclass
class Reservation:
    trader: str
    ticker: str
    side: Side
    cash_left: int = 0
    shares_left: int = 0
    released: bool = False


@dataclass
class Ledger:
    cash: dict[str, int] = field(default_factory=lambda: dict.fromkeys(TRADERS, START_CASH))
    reserved_cash: dict[str, int] = field(default_factory=lambda: dict.fromkeys(TRADERS, 0))
    shares: dict[tuple[str, str], int] = field(
        default_factory=lambda: {(t, k): START_SHARES for t in TRADERS for k in TICKERS}
    )
    reserved_shares: dict[tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))
    reservations: dict[str, Reservation] = field(default_factory=dict)
    last_price: dict[str, int] = field(default_factory=lambda: dict(REFERENCE))
    problems: list[str] = field(default_factory=list)

    def admit(self, order: PlaceOrder) -> bool:
        """A-06: reserve, or reject (no margin, no short selling)."""
        if order.side is Side.BUY:
            price = order.limit_price_paise or order.protection_price_paise or 0
            need = order.quantity * price
            if self.cash[order.trader_id] - self.reserved_cash[order.trader_id] < need:
                return False
            self.reserved_cash[order.trader_id] += need
            self.reservations[order.order_id] = Reservation(
                order.trader_id, order.ticker, order.side, cash_left=need
            )
        else:
            key = (order.trader_id, order.ticker)
            if self.shares[key] - self.reserved_shares[key] < order.quantity:
                return False
            self.reserved_shares[key] += order.quantity
            self.reservations[order.order_id] = Reservation(
                order.trader_id, order.ticker, order.side, shares_left=order.quantity
            )
        return True

    def _release(self, order_id: str) -> None:
        r = self.reservations.get(order_id)
        if r is None or r.released:
            return
        self.reserved_cash[r.trader] -= r.cash_left
        self.reserved_shares[(r.trader, r.ticker)] -= r.shares_left
        r.cash_left = r.shares_left = 0
        r.released = True

    def apply(self, event: MarketEvent) -> None:
        if isinstance(event, TradeExecuted):
            self._settle(event)
        elif isinstance(event, OrderCancelled):
            r = self.reservations.get(event.order_id)
            if r is not None and r.side is Side.SELL and r.shares_left != event.cancelled_quantity:
                self.problems.append(f"cancel of {event.order_id}: reserved shares != cancelled")
            self._release(event.order_id)
        elif isinstance(event, OrderExpired):
            self._release(event.order_id)

    def _settle(self, t: TradeExecuted) -> None:
        cost = t.quantity * t.price_paise
        buy, sell = self.reservations.get(t.buy_order_id), self.reservations.get(t.sell_order_id)
        if buy is None or sell is None or buy.released or sell.released:
            self.problems.append(f"trade {t.trade_id} uses money or shares that are not reserved")
            return
        if buy.cash_left < cost:
            self.problems.append(f"trade {t.trade_id} costs more than the buyer reserved")
        if sell.shares_left < t.quantity:
            self.problems.append(f"trade {t.trade_id} sells more than the seller reserved")
        buy.cash_left -= cost
        self.reserved_cash[buy.trader] -= cost
        self.cash[buy.trader] -= cost
        self.shares[(buy.trader, t.ticker)] += t.quantity
        sell.shares_left -= t.quantity
        self.reserved_shares[(sell.trader, t.ticker)] -= t.quantity
        self.shares[(sell.trader, t.ticker)] -= t.quantity
        self.cash[sell.trader] += cost
        self.last_price[t.ticker] = t.price_paise
        if t.buy_order_remaining_quantity == 0:
            self._release(t.buy_order_id)
        if t.sell_order_remaining_quantity == 0:
            self._release(t.sell_order_id)

    def negative_or_over_reserved(self) -> list[str]:
        bad = []
        for trader in TRADERS:
            if not 0 <= self.reserved_cash[trader] <= self.cash[trader]:
                bad.append(
                    f"{trader}: cash {self.cash[trader]} reserved {self.reserved_cash[trader]}"
                )
            for ticker in TICKERS:
                held, reserved = (
                    self.shares[(trader, ticker)],
                    self.reserved_shares[(trader, ticker)],
                )
                if not 0 <= reserved <= held:
                    bad.append(f"{trader} {ticker}: shares {held}, reserved {reserved}")
        return bad

    def total_cash(self) -> int:
        return sum(self.cash.values())

    def total_shares(self, ticker: str) -> int:
        return sum(self.shares[(t, ticker)] for t in TRADERS)


@dataclass
class Workload:
    """Turns actions into engine commands, using the ledger as order-service and A-06 would."""

    ledger: Ledger = field(default_factory=Ledger)
    placed: list[PlaceOrder] = field(default_factory=list)
    expired_never_placed: list[tuple[str, str, str]] = field(
        default_factory=list
    )  # id, trader, ticker
    next_order: int = 1
    next_command: int = 1
    rejected: int = 0

    def _command_id(self) -> str:
        self.next_command += 1
        return f"c1c1c1c1-0000-4000-8000-{self.next_command:012d}"

    def command_for(self, action: Action) -> Command | None:
        if isinstance(action, Place):
            return self._place(action)
        if isinstance(action, Cancel):
            if not self.placed:
                return None
            order = self.placed[action.ref % len(self.placed)]
            owner = TRADERS.index(order.trader_id)
            trader = order.trader_id if action.by_owner else TRADERS[(owner + 1) % len(TRADERS)]
            return CancelOrder(
                command_id=self._command_id(),
                correlation_id="cancel",
                ticker=order.ticker,
                order_id=order.order_id,
                trader_id=trader,
                requested_at=T0,
            )
        if isinstance(action, Expire):
            if action.never_placed or not self.placed:
                order_id = oid(900_000 + self.next_order)
                self.next_order += 1
                trader, ticker = TRADERS[action.ref % 4], TICKERS[action.ref % 2]
                self.expired_never_placed.append((order_id, trader, ticker))
                return self._expire(order_id, trader, ticker)
            order = self.placed[action.ref % len(self.placed)]
            return self._expire(order.order_id, order.trader_id, order.ticker)
        if not self.expired_never_placed:
            return None
        dead = self.expired_never_placed
        order_id, trader, ticker = dead[action.ref % len(dead)]
        return PlaceOrder(
            command_id=self._command_id(),
            correlation_id="late",
            ticker=ticker,
            order_id=order_id,
            trader_id=trader,
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            quantity=1,
            limit_price_paise=REFERENCE[ticker],
            protection_price_paise=None,
            placed_at=T0,
        )

    def _expire(self, order_id: str, trader: str, ticker: str) -> ExpireOrder:
        return ExpireOrder(
            command_id=self._command_id(),
            correlation_id="expiry",
            ticker=ticker,
            order_id=order_id,
            trader_id=trader,
            requested_at=T0,
        )

    def _place(self, action: Place) -> PlaceOrder | None:
        ticker = TICKERS[action.ticker]
        side = Side(action.side)
        if action.is_market:
            limit_price, protection = None, protection_price(self.ledger.last_price[ticker], side)
        else:
            limit_price = REFERENCE[ticker] + TICK * action.ticks_from_reference
            protection = None
        order = PlaceOrder(
            command_id=self._command_id(),
            correlation_id=f"req-{self.next_order}",
            ticker=ticker,
            order_id=oid(self.next_order),
            trader_id=TRADERS[action.trader],
            side=side,
            order_type=OrderType.MARKET if action.is_market else OrderType.LIMIT,
            quantity=action.quantity,
            limit_price_paise=limit_price,
            protection_price_paise=protection,
            placed_at=T0,
        )
        self.next_order += 1
        if not self.ledger.admit(order):
            self.rejected += 1
            return None
        self.placed.append(order)
        return order
