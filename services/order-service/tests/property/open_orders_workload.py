"""A random life of orders, for the open-orders property (Q-07 status=open).

Traders place orders (some refused for lack of cash, some while Redpanda is down so they wait in the
outbox), ask to cancel them, and the engine answers in any order: accepted, traded (part or all),
cancelled, cancel refused, expired. The relay rejects an order not published within 60 s, and a late
OrderAccepted opens a rejected order again. After every step the open-orders table, read through the
store's own `open_orders_of`, must hold exactly the orders that are not finished: nothing missing,
nothing closed. The same workload runs against the in-memory store (tests/property) and the real
Cassandra store (tests/integration).
"""

import uuid
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

from hypothesis import strategies as st

from order_service.application.orders import Orders, RejectedOrderError
from order_service.application.outbox_relay import OutboxRelay
from order_service.domain.order import OrderRequest, OrderType, Side
from order_service.ports.order_store import OrderStore
from order_service.runtime.events import build_event
from tests.fakes import FakePortfolio, RecordingPublisher

ORDER = st.integers(min_value=0, max_value=7)  # the n-th order placed (mod how many exist)
step = st.one_of(
    st.tuples(st.just("place"), st.integers(0, 2), st.sampled_from([5, 20, 400]), st.booleans()),
    st.tuples(st.just("accept"), ORDER),
    st.tuples(st.just("trade"), ORDER, st.integers(1, 30)),
    st.tuples(st.just("cancel"), ORDER),
    st.tuples(st.just("cancelled"), ORDER),
    st.tuples(st.just("cancel_rejected"), ORDER),
    st.tuples(st.just("expired"), ORDER),
    st.tuples(st.just("relay"), st.sampled_from([3, 30, 61])),
)
workloads = st.lists(step, min_size=1, max_size=30)


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC).replace(microsecond=0)

    def __call__(self) -> datetime:
        return self.now


async def run(store: OrderStore, workload: list[tuple[Any, ...]]) -> None:
    """Play the workload; after each step, the open-orders table equals the open orders."""
    clock = Clock()
    publisher = RecordingPublisher()
    portfolio = FakePortfolio(budget_paise=1_000_000)  # ₹10,000: 400 shares at ₹246 is refused
    orders = Orders(store, portfolio, publisher, environment="dev", clock=clock)
    relay = OutboxRelay(store, publisher, clock=clock)
    traders = [str(uuid.uuid4()) for _ in range(3)]  # fresh per run: a shared store stays apart
    placed: list[str] = []
    sequence = count(1)

    async def engine(kind: str, **data: object) -> None:
        event = build_event(
            kind,
            {"sequence": next(sequence), "ticker": "CALVERA", **data},
            producer="matching-engine",
            environment="dev",
            correlation_id="property",
        )
        await orders.apply_matching_event(event)

    for action, *args in workload:
        clock.now += timedelta(milliseconds=5)  # every order gets its own placed_at
        if action == "place":
            trader, quantity, broker_up = args
            publisher.down = not broker_up
            request = OrderRequest("CALVERA", Side.BUY, OrderType.LIMIT, quantity, 24600)
            try:
                new = await orders.place(traders[trader], request, correlation_id="p")
                placed.append(new.order_id)
            except RejectedOrderError as refused:
                placed.append(refused.order.order_id)
            publisher.down = False
        elif action == "relay":
            clock.now += timedelta(seconds=args[0])
            await relay.run_once()
        elif placed:
            order_id = placed[args[0] % len(placed)]
            order = await store.get(order_id)
            assert order is not None
            if action == "accept":
                await engine("OrderAccepted", order_id=order_id)
            elif action == "trade":
                quantity = min(args[1], max(order.remaining_quantity, 1))
                await engine(
                    "TradeExecuted",
                    trade_id=str(uuid.uuid4()),
                    buy_order_id=order_id,
                    sell_order_id=str(uuid.uuid4()),  # a seller this service does not know
                    quantity=quantity,
                    price_paise=24600,
                    buy_order_remaining_quantity=max(order.remaining_quantity - quantity, 0),
                    sell_order_remaining_quantity=0,
                )
            elif action == "cancel":
                if order.cancellable():
                    await orders.cancel(order.trader_id, order_id, correlation_id="c")
            elif action == "cancelled":
                await engine("OrderCancelled", order_id=order_id, reason="USER_REQUESTED")
            elif action == "cancel_rejected":
                await engine("OrderCancelRejected", order_id=order_id)
            else:
                await engine("OrderExpired", order_id=order_id)
        await check(store, traders, placed)


async def check(store: OrderStore, traders: list[str], placed: list[str]) -> None:
    expected = set()
    for order_id in placed:
        order = await store.get(order_id)
        if order is not None and order.is_open:
            expected.add(order_id)
    listed: list[str] = []
    for trader in traders:
        mine = await store.open_orders_of(trader, before=None, limit=1_000)
        assert [o.placed_at for o in mine] == sorted((o.placed_at for o in mine), reverse=True)
        listed += [o.order_id for o in mine]
    assert len(listed) == len(set(listed)), "an order is listed twice"
    assert set(listed) == expected, (sorted(set(listed) - expected), sorted(expected - set(listed)))
