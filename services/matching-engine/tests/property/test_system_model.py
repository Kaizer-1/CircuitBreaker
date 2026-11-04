"""System-model property test of the fail-closed orphan rule (ADR-0017).

The matching engine here is the real one (domain + application, with an in-memory store). Around it
are small models of the other two services, written from the docs:

- order-service: reserves (A-06) then stores the order in its outbox, and may crash in between. Its
  relay publishes, never publishes an OrderPlaced older than 60 s (REJECTED / PUBLISH_TIMEOUT), and
  sometimes "hangs", so a publish lands much later (a zombie). A-19 writes an expiry request.
- portfolio-service: reservations, settlement from matching.v1 with a consumer that falls behind,
  and the sweeper that asks for an expiry after 120 s (only when its partition has no lag).

Topics are one ordered log per ticker (the message key), as on Redpanda. Every service can be down
for a while; the engine can fail to publish and can be restarted. The safety property: **no trade is
ever settled against a reservation that was already released.** The liveness property: once
everything is up again, no unconfirmed reservation stays locked.

The same model with ADR-0011's rule (release after 120 s by timer) is shown to be unsafe.
"""

import asyncio
import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from hypothesis import find, given, settings
from hypothesis import strategies as st

from matching_engine.application.engine import MatchingEngine
from matching_engine.runtime.events import EventEnvelope, build_event, deterministic_event_id
from tests.builders import ALICE, BOB, CARA, DEV, id_source
from tests.fakes import InMemoryEventStore

TRADERS = (ALICE, BOB, CARA, DEV)
TICKERS = ("CHAIWALA", "JUGAAD")
PRICE = {"CHAIWALA": 24550, "JUGAAD": 118000}
START = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)
PUBLISH_TIMEOUT_S, ORPHAN_AFTER_S = 60, 120

Logs = dict[str, list[EventEnvelope]]  # ticker -> ordered partition


def iso(seconds: int) -> str:
    return (
        (START + timedelta(seconds=seconds))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def order_placed(
    order_id: str, trader: str, ticker: str, side: str, qty: int, price: int, now: int
) -> EventEnvelope:
    data = {
        "order_id": order_id,
        "trader_id": trader,
        "ticker": ticker,
        "side": side,
        "order_type": "LIMIT",
        "quantity": qty,
        "limit_price_paise": price,
        "protection_price_paise": None,
        "reservation_id": "c1d2e3f4-a5b6-4c7d-8e9f-0a1b2c3d4e5f",
        "placed_at": iso(now),
    }
    return build_event(
        "OrderPlaced", data, producer="order-service", environment="dev", correlation_id=order_id
    )


@dataclass
class Clock:
    now: int = 0  # seconds since START


@dataclass
class Outbox:
    event: EventEnvelope
    ticker: str
    placed_at: int | None  # only for OrderPlaced
    done: bool = False


@dataclass
class OrderServiceModel:
    clock: Clock
    orders: Logs
    up: bool = True
    outbox: list[Outbox] = field(default_factory=list)
    in_flight: list[tuple[int, str, EventEnvelope]] = field(default_factory=list)  # (lands_at, ...)
    rejected: set[str] = field(default_factory=set)

    def store(self, event: EventEnvelope, ticker: str, placed_at: int | None) -> None:
        self.outbox.append(Outbox(event, ticker, placed_at))

    def request_expiry(self, order_id: str, trader: str, ticker: str) -> bool:
        """A-19. False when order-service is down (the sweeper retries on its next cycle)."""
        if not self.up:
            return False
        data = {
            "order_id": order_id,
            "trader_id": trader,
            "ticker": ticker,
            "requested_at": iso(self.clock.now),
        }
        event = build_event(
            "OrderExpiryRequested",
            data,
            producer="order-service",
            environment="dev",
            correlation_id=f"expiry-{order_id}",
            event_id=deterministic_event_id(
                f"https://circuitbreaker.local/order-expiry/{order_id}"
            ),
        )
        self.store(event, ticker, None)
        return True

    def relay(self, rng: random.Random) -> None:
        if not self.up:
            return
        for entry in self.outbox:
            if entry.done:
                continue
            entry.done = True
            if entry.placed_at is not None and self.clock.now - entry.placed_at > PUBLISH_TIMEOUT_S:
                self.rejected.add(entry.event.data["order_id"])  # REJECTED, PUBLISH_TIMEOUT
                continue
            if rng.random() < 0.15:  # the send hangs and lands much later: a zombie publish
                self.in_flight.append(
                    (self.clock.now + rng.randint(1, 400), entry.ticker, entry.event)
                )
            else:
                self.orders[entry.ticker].append(entry.event)

    def land_in_flight(self) -> None:
        landed = [m for m in self.in_flight if m[0] <= self.clock.now]
        self.in_flight = [m for m in self.in_flight if m[0] > self.clock.now]
        for _, ticker, event in landed:
            self.orders[ticker].append(event)


@dataclass
class Reservation:
    trader: str
    ticker: str
    side: str
    cash_left: int
    shares_left: int
    created_at: int
    confirmed: bool = False
    released: bool = False
    expiry_requested: bool = False


@dataclass
class PortfolioModel:
    clock: Clock
    matching: Logs
    fail_closed: bool
    up: bool = True
    cash: dict[str, int] = field(default_factory=lambda: dict.fromkeys(TRADERS, 30_000_000))
    reserved_cash: dict[str, int] = field(default_factory=lambda: dict.fromkeys(TRADERS, 0))
    shares: dict[tuple[str, str], int] = field(default_factory=lambda: defaultdict(lambda: 200))
    reserved_shares: dict[tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))
    reservations: dict[str, Reservation] = field(default_factory=dict)
    position: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    processed: set[str] = field(default_factory=set)
    violations: list[str] = field(default_factory=list)

    def reserve(
        self, order_id: str, trader: str, ticker: str, side: str, qty: int, price: int
    ) -> bool:
        if side == "BUY":
            if self.cash[trader] - self.reserved_cash[trader] < qty * price:
                return False
            self.reserved_cash[trader] += qty * price
            r = Reservation(trader, ticker, side, qty * price, 0, self.clock.now)
        else:
            if self.shares[(trader, ticker)] - self.reserved_shares[(trader, ticker)] < qty:
                return False
            self.reserved_shares[(trader, ticker)] += qty
            r = Reservation(trader, ticker, side, 0, qty, self.clock.now)
        self.reservations[order_id] = r
        return True

    def release(self, order_id: str) -> None:
        r = self.reservations.get(order_id)
        if r is None or r.released:
            return
        self.reserved_cash[r.trader] -= r.cash_left
        self.reserved_shares[(r.trader, r.ticker)] -= r.shares_left
        r.cash_left = r.shares_left = 0
        r.released = True

    def consume(self, budget: int) -> None:
        if not self.up:
            return
        for ticker in TICKERS:
            log = self.matching[ticker]
            for _ in range(budget):
                if self.position[ticker] >= len(log):
                    break
                event = log[self.position[ticker]]
                self.position[ticker] += 1
                if event.event_id not in self.processed:
                    self.processed.add(event.event_id)
                    self.apply(event)

    def apply(self, event: EventEnvelope) -> None:
        d = event.data
        if event.event_type == "OrderAccepted" and d["order_id"] in self.reservations:
            self.reservations[d["order_id"]].confirmed = True
        elif event.event_type == "TradeExecuted":
            self.settle(d)
        elif event.event_type in ("OrderCancelled", "OrderExpired"):
            self.release(d["order_id"])

    def settle(self, d: dict[str, Any]) -> None:
        qty, price = d["quantity"], d["price_paise"]
        buy = self.reservations.get(d["buy_order_id"])
        sell = self.reservations.get(d["sell_order_id"])
        for name, r in (("buy", buy), ("sell", sell)):
            if r is None or r.released:
                self.violations.append(
                    f"trade {d['trade_id']}: {name} order's reservation was released"
                )
                return
        assert buy is not None and sell is not None
        if buy.cash_left < qty * price or sell.shares_left < qty:
            self.violations.append(f"trade {d['trade_id']} is not covered by its reservations")
            return
        buy.cash_left -= qty * price
        self.reserved_cash[buy.trader] -= qty * price
        self.cash[buy.trader] -= qty * price
        self.shares[(buy.trader, buy.ticker)] += qty
        sell.shares_left -= qty
        self.reserved_shares[(sell.trader, sell.ticker)] -= qty
        self.shares[(sell.trader, sell.ticker)] -= qty
        self.cash[sell.trader] += qty * price
        if d["buy_order_remaining_quantity"] == 0:
            self.release(d["buy_order_id"])
        if d["sell_order_remaining_quantity"] == 0:
            self.release(d["sell_order_id"])

    def sweep(self, order_service: OrderServiceModel) -> None:
        if not self.up:
            return
        for order_id, r in self.reservations.items():
            if r.confirmed or r.released or r.expiry_requested:
                continue
            if self.clock.now - r.created_at < ORPHAN_AFTER_S:
                continue
            if not self.fail_closed:  # ADR-0011: release by timer
                self.release(order_id)
                continue
            if self.position[r.ticker] < len(self.matching[r.ticker]):
                continue  # behind on this partition: an OrderAccepted may be unread
            r.expiry_requested = order_service.request_expiry(order_id, r.trader, r.ticker)


class LogPublisher:
    def __init__(self, matching: Logs) -> None:
        self.matching, self.failing = matching, False

    async def publish(self, ticker: str, event: EventEnvelope) -> None:
        if self.failing:
            raise ConnectionError("redpanda unreachable")
        self.matching[ticker].append(event)


@dataclass
class Result:
    violations: list[str]
    stuck: list[str]
    trades: int
    orphans_released: int


async def simulate(seed: int, fail_closed: bool, ticks: int = 120) -> Result:
    rng = random.Random(seed)
    clock = Clock()
    orders: Logs = defaultdict(list)
    matching: Logs = defaultdict(list)
    os_model = OrderServiceModel(clock, orders)
    ps = PortfolioModel(clock, matching, fail_closed)
    store, publisher = InMemoryEventStore(), LogPublisher(matching)
    new_id = id_source("5e5e5e5e")
    engine_position: dict[str, int] = defaultdict(int)
    down_until = {"os": 0, "ps": 0, "engine": 0}
    order_no = 0

    async def boot() -> MatchingEngine:
        engine = MatchingEngine(
            store,
            publisher,
            environment="dev",
            snapshot_every=5,
            clock=lambda: START + timedelta(seconds=clock.now),
            new_id=new_id,
        )
        await engine.recover()
        return engine

    engine = await boot()

    async def engine_step(budget: int) -> None:
        for ticker in TICKERS:
            for _ in range(budget):
                if engine_position[ticker] >= len(orders[ticker]):
                    break
                try:
                    await engine.handle_envelope(orders[ticker][engine_position[ticker]])
                except ConnectionError:
                    break  # not committed: redelivered next time
                engine_position[ticker] += 1

    for _ in range(ticks):
        clock.now += rng.randint(1, 25)
        for name in down_until:
            if rng.random() < 0.04:
                down_until[name] = clock.now + rng.randint(10, 240)
        os_model.up = clock.now >= down_until["os"]
        ps.up = clock.now >= down_until["ps"]
        engine_up = clock.now >= down_until["engine"]
        publisher.failing = rng.random() < 0.1

        if os_model.up and ps.up and rng.random() < 0.7:  # a trader places an order
            order_no += 1
            ticker, trader = rng.choice(TICKERS), rng.choice(TRADERS)
            side, qty = rng.choice(["BUY", "SELL"]), rng.randint(1, 40)
            price = PRICE[ticker] + 5 * rng.randint(-4, 4)
            order_id = f"0dde0000-0000-4000-8000-{order_no:012d}"
            if ps.reserve(order_id, trader, ticker, side, qty, price):
                if rng.random() < 0.15:  # order-service dies after reserving, before storing
                    down_until["os"] = clock.now + rng.randint(5, 200)
                    os_model.up = False
                else:
                    placed = order_placed(order_id, trader, ticker, side, qty, price, clock.now)
                    os_model.store(placed, ticker, clock.now)

        os_model.relay(rng)
        os_model.land_in_flight()
        if engine_up:
            if rng.random() < 0.05 and not publisher.failing:  # killed and restarted
                engine = await boot()
            await engine_step(rng.randint(0, 6))
        ps.consume(rng.choice([0, 1, 3, 10]))
        ps.sweep(os_model)

    # Everything comes back and stays up: let every queue drain.
    os_model.up = ps.up = True
    publisher.failing = False
    for _ in range(60):
        clock.now += 10
        os_model.relay(rng)
        os_model.land_in_flight()
        await engine_step(1000)
        ps.consume(1000)
        ps.sweep(os_model)

    stuck = [oid for oid, r in ps.reservations.items() if not r.confirmed and not r.released]
    trades = sum(1 for log in matching.values() for e in log if e.event_type == "TradeExecuted")
    orphans = sum(1 for r in ps.reservations.values() if r.released and not r.confirmed)
    return Result(ps.violations, stuck, trades, orphans)


@given(st.integers(min_value=0, max_value=2**32))
def test_fail_closed_no_trade_ever_settles_against_released_money(seed: int) -> None:
    result = asyncio.run(simulate(seed, fail_closed=True))
    assert result.violations == []


@given(st.integers(min_value=0, max_value=2**32))
def test_fail_closed_no_unconfirmed_reservation_stays_locked_once_all_is_up(seed: int) -> None:
    result = asyncio.run(simulate(seed, fail_closed=True))
    assert result.stuck == []


def test_the_old_120_second_timer_rule_can_trade_on_released_money() -> None:
    # Not a property: Hypothesis searches for ONE seed where ADR-0011's timer breaks "no margin".
    unsafe = find(
        st.integers(min_value=0, max_value=2**32),
        lambda seed: bool(asyncio.run(simulate(seed, fail_closed=False)).violations),
        settings=settings(max_examples=2000, database=None, derandomize=True),
    )
    assert asyncio.run(simulate(unsafe, fail_closed=False)).violations
