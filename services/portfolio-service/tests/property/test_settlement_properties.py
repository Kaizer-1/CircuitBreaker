"""Property-based tests (Hypothesis): settlement under random workloads and redelivered events.

A small model of the engine produces consistent matching.v1 events (fills within both orders'
remaining quantity and limits, cancels, expiries); some events are delivered again later, as
at-least-once delivery allows. The real use cases apply them to the in-memory store, whose save
methods enforce the same CHECK constraints as Postgres (nothing negative, reserved <= owned).
"""

import asyncio
from dataclasses import dataclass

from hypothesis import given
from hypothesis import strategies as st

from portfolio_service.application.portfolio import PortfolioService
from portfolio_service.domain.model import (
    AccountKind,
    InsufficientFundsError,
    InsufficientSharesError,
    Side,
)
from portfolio_service.runtime.events import EventEnvelope
from tests.builders import accepted, buy, cancelled, expired, oid, sell, trade
from tests.fakes import InMemoryPortfolioStore

HUMANS = ("Raju", "Meera", "Asha")
BOTS = ("bot-01", "bot-02")
TICKER, REFERENCE = "CALVERA", 24550


@dataclass(frozen=True)
class Place:
    who: int
    side: str
    is_market: bool
    quantity: int
    ticks: int


@dataclass(frozen=True)
class Fill:
    buy_ref: int
    sell_ref: int
    seed: int
    at_buy_price: bool


@dataclass(frozen=True)
class Finish:
    ref: int
    expire: bool
    reason: str


@dataclass(frozen=True)
class Redeliver:
    ref: int


Action = Place | Fill | Finish | Redeliver
_place = st.builds(
    Place,
    who=st.integers(0, len(HUMANS) + len(BOTS) - 1),
    side=st.sampled_from(["BUY", "SELL"]),
    is_market=st.sampled_from([False, False, False, True]),
    quantity=st.integers(1, 120),
    ticks=st.integers(-6, 6),
)
_fill = st.builds(Fill, st.integers(0, 99), st.integers(0, 99), st.integers(0, 999), st.booleans())
_finish = st.builds(
    Finish,
    st.integers(0, 99),
    st.booleans(),
    st.sampled_from(
        ["USER_REQUESTED", "SELF_TRADE_PREVENTED", "UNFILLED_MARKET_ORDER", "ORDER_EXPIRED"]
    ),
)
actions = st.lists(
    st.one_of(_place, _place, _fill, _fill, _finish, st.builds(Redeliver, st.integers(0, 999))),
    max_size=50,
)


@dataclass
class Open:
    order_id: int
    trader: str
    side: Side
    remaining: int
    price: int  # limit, or protection price for MARKET


async def run(
    workload: list[Action], *, redeliver: bool
) -> tuple[PortfolioService, list[str], int]:
    service = PortfolioService(InMemoryPortfolioStore())
    traders = [(await service.open_account(h, AccountKind.HUMAN))[0].trader_id for h in HUMANS]
    traders += [(await service.open_account(b, AccountKind.BOT))[0].trader_id for b in BOTS]
    open_orders: list[Open] = []
    delivered: list[EventEnvelope] = []
    next_order = 0

    async def deliver(event: EventEnvelope) -> None:
        assert await service.apply_event(event)  # a new event is always applied
        delivered.append(event)

    for action in workload:
        if isinstance(action, Place):
            next_order += 1
            trader = traders[action.who]
            price = None if action.is_market else REFERENCE + 5 * action.ticks
            request = (buy if action.side == "BUY" else sell)(
                next_order, trader, action.quantity, price
            )
            try:
                r, _ = await service.create_reservation(request)
            except (InsufficientFundsError, InsufficientSharesError):
                continue  # 422: never reaches the engine
            limit = (
                r.limit_price_paise if r.limit_price_paise is not None else r.protection_price_paise
            )
            assert limit is not None
            open_orders.append(Open(next_order, trader, Side(action.side), action.quantity, limit))
            await deliver(accepted(next_order, trader))
        elif isinstance(action, Fill):
            buys = [o for o in open_orders if o.side is Side.BUY]
            sells = [o for o in open_orders if o.side is Side.SELL]
            if not buys or not sells:
                continue
            b, s = buys[action.buy_ref % len(buys)], sells[action.sell_ref % len(sells)]
            if b.trader == s.trader or b.price < s.price:
                continue  # the engine never self-trades, and only crossing orders match
            qty = 1 + action.seed % min(b.remaining, s.remaining)
            price = b.price if action.at_buy_price else s.price  # a resting order's price
            b.remaining, s.remaining = b.remaining - qty, s.remaining - qty
            await deliver(
                trade(
                    b.order_id, b.trader, s.order_id, s.trader, qty, price, b.remaining, s.remaining
                )
            )
            open_orders = [o for o in open_orders if o.remaining > 0]
        elif isinstance(action, Finish):
            if not open_orders:
                continue
            o = open_orders.pop(action.ref % len(open_orders))
            event = (
                expired(o.order_id, o.trader)
                if action.expire
                else cancelled(o.order_id, o.trader, o.remaining, action.reason)
            )
            await deliver(event)
        elif redeliver and delivered:
            assert await service.apply_event(delivered[action.ref % len(delivered)]) is False
    return service, traders, len(open_orders)


async def balances(
    service: PortfolioService, traders: list[str]
) -> list[tuple[int, int, int, int]]:
    out = []
    for t in traders:
        view = await service.portfolio(t)
        held = {h["ticker"]: h for h in view["holdings"]}.get(
            TICKER, {"quantity": 0, "reserved_quantity": 0}
        )
        out.append(
            (
                view["cash_paise"],
                view["reserved_cash_paise"],
                held["quantity"],
                held["reserved_quantity"],
            )
        )
    return out


@given(actions)
def test_redelivered_events_never_change_balances(workload: list[Action]) -> None:
    async def go() -> None:
        once, traders, _ = await run(workload, redeliver=False)
        twice, traders2, _ = await run(workload, redeliver=True)
        assert await balances(once, traders) == await balances(twice, traders2)

    asyncio.run(go())


@given(actions)
def test_trading_conserves_cash_and_shares_and_nothing_goes_negative(
    workload: list[Action],
) -> None:
    async def go() -> None:
        service, traders, _ = await run(workload, redeliver=True)
        start = await run([], redeliver=False)
        now, before = await balances(service, traders), await balances(start[0], start[1])
        assert sum(c for c, _, _, _ in now) == sum(c for c, _, _, _ in before)
        assert sum(q for _, _, q, _ in now) == sum(q for _, _, q, _ in before)
        for cash, reserved_cash, quantity, reserved_quantity in now:
            assert cash >= reserved_cash >= 0
            assert quantity >= reserved_quantity >= 0

    asyncio.run(go())


@given(actions)
def test_once_every_order_has_finished_nothing_stays_reserved(workload: list[Action]) -> None:
    async def go() -> None:
        service, traders, still_open = await run(
            workload + [Finish(0, True, "")] * 200, redeliver=True
        )
        assert still_open == 0
        for _, reserved_cash, _, reserved_quantity in await balances(service, traders):
            assert (reserved_cash, reserved_quantity) == (0, 0)

    asyncio.run(go())


def test_the_order_ids_of_this_model_match_the_builders() -> None:
    assert oid(1).endswith("000000000001")
