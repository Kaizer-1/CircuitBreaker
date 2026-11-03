"""The fail-closed orphan sweeper (policy P-15, ADR-0017)."""

from datetime import UTC, datetime, timedelta

import pytest

from portfolio_service.application.portfolio import PortfolioService
from portfolio_service.application.sweeper import OrphanSweeper
from portfolio_service.domain.model import AccountKind
from tests.builders import accepted, buy, expired, oid
from tests.fakes import FakeOrderService, InMemoryPortfolioStore

PLACED = datetime(2026, 11, 20, 10, 42, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = PLACED

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store() -> InMemoryPortfolioStore:
    return InMemoryPortfolioStore()


@pytest.fixture
def orders() -> FakeOrderService:
    return FakeOrderService()


async def orphan(store: InMemoryPortfolioStore, clock: Clock) -> tuple[PortfolioService, str]:
    """Raju reserves 50 x 24600 for order 1; order-service then dies before sending it."""
    service = PortfolioService(store, clock=clock)
    account, _ = await service.open_account("RajuTrader", AccountKind.HUMAN)
    await service.create_reservation(buy(1, account.trader_id, 50, 24600))
    return service, account.trader_id


def sweeper(
    store: InMemoryPortfolioStore, orders: FakeOrderService, clock: Clock, can: bool = True
) -> OrphanSweeper:
    return OrphanSweeper(store, orders, can_sweep=lambda ticker: can, clock=clock)


async def test_nothing_is_asked_before_120_seconds(
    store: InMemoryPortfolioStore, orders: FakeOrderService, clock: Clock
) -> None:
    await orphan(store, clock)
    clock.now += timedelta(seconds=119)
    assert await sweeper(store, orders, clock).sweep_once() == 0
    assert orders.requests == []


async def test_after_120_seconds_it_asks_for_an_expiry_but_releases_nothing(
    store: InMemoryPortfolioStore, orders: FakeOrderService, clock: Clock
) -> None:
    service, raju = await orphan(store, clock)
    clock.now += timedelta(seconds=120)
    assert await sweeper(store, orders, clock).sweep_once() == 1
    assert orders.requests == [(oid(1), raju, "CHAIWALA")]
    # Fail-closed: the money is still reserved until the engine answers.
    assert (await service.portfolio(raju))["reserved_cash_paise"] == 1_230_000
    assert store.state.reservations[oid(1)].expiry_requested_at is not None


async def test_the_money_is_released_only_when_the_engine_answers(
    store: InMemoryPortfolioStore, orders: FakeOrderService, clock: Clock
) -> None:
    service, raju = await orphan(store, clock)
    clock.now += timedelta(seconds=130)
    await sweeper(store, orders, clock).sweep_once()
    await service.apply_event(expired(1, raju))  # the engine never saw it: a tombstone
    assert (await service.portfolio(raju))["reserved_cash_paise"] == 0


async def test_an_expiry_is_asked_for_once(
    store: InMemoryPortfolioStore, orders: FakeOrderService, clock: Clock
) -> None:
    await orphan(store, clock)
    clock.now += timedelta(seconds=130)
    s = sweeper(store, orders, clock)
    await s.sweep_once()
    clock.now += timedelta(seconds=60)
    assert await s.sweep_once() == 0
    assert len(orders.requests) == 1


async def test_if_order_service_is_down_the_next_cycle_asks_again(
    store: InMemoryPortfolioStore, orders: FakeOrderService, clock: Clock
) -> None:
    service, raju = await orphan(store, clock)
    clock.now += timedelta(seconds=130)
    orders.down = True
    s = sweeper(store, orders, clock)
    assert await s.sweep_once() == 0
    assert store.state.reservations[oid(1)].expiry_requested_at is None
    orders.down = False
    clock.now += timedelta(seconds=10)
    assert await s.sweep_once() == 1
    assert (await service.portfolio(raju))["reserved_cash_paise"] == 1_230_000


async def test_a_confirmed_order_is_never_an_orphan(
    store: InMemoryPortfolioStore, orders: FakeOrderService, clock: Clock
) -> None:
    service, raju = await orphan(store, clock)
    await service.apply_event(accepted(1, raju))
    clock.now += timedelta(hours=1)
    assert await sweeper(store, orders, clock).sweep_once() == 0


async def test_tickers_whose_partition_is_behind_or_not_ours_are_skipped(
    store: InMemoryPortfolioStore, orders: FakeOrderService, clock: Clock
) -> None:
    await orphan(store, clock)
    clock.now += timedelta(seconds=130)
    assert await sweeper(store, orders, clock, can=False).sweep_once() == 0
    assert orders.requests == []


async def test_a_reservation_released_while_asking_is_left_alone(
    store: InMemoryPortfolioStore, orders: FakeOrderService, clock: Clock
) -> None:
    service, raju = await orphan(store, clock)
    clock.now += timedelta(seconds=130)

    class AnswerFast(FakeOrderService):
        async def request_expiry(self, order_id: str, trader_id: str, ticker: str) -> None:
            await super().request_expiry(order_id, trader_id, ticker)
            await service.apply_event(expired(1, raju))  # the answer arrives before we record it

    assert await sweeper(store, AnswerFast(), clock).sweep_once() == 1
    assert store.state.reservations[oid(1)].expiry_requested_at is None
    assert (await service.portfolio(raju))["reserved_cash_paise"] == 0


async def test_a_failing_sweep_is_logged_and_the_loop_goes_on(
    store: InMemoryPortfolioStore,
    orders: FakeOrderService,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def broken() -> int:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt  # stop the endless loop after the failure was survived
        raise OSError("database down")

    s = sweeper(store, orders, clock)
    monkeypatch.setattr(s, "sweep_once", broken)
    with pytest.raises(KeyboardInterrupt):
        await s.run(every_seconds=0)
    assert calls == 2
