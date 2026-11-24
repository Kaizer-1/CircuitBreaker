"""A-21/A-22: "Portfolio database degraded" (chaos scenarios section 3.4)."""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from portfolio_service.application.faults import DatabaseDegraded, FaultAlreadyActiveError
from portfolio_service.application.portfolio import PortfolioService
from portfolio_service.config import Settings
from portfolio_service.domain.model import AccountKind
from portfolio_service.main import create_app
from tests.builders import buy
from tests.fakes import InMemoryPortfolioStore

DEGRADE = {"fault": "DB_DEGRADED", "delay_ms": 1500}
NOW = datetime(2026, 11, 25, 9, 0, tzinfo=UTC)


async def test_while_on_every_reservation_waits_in_the_database_and_a_restart_finds_it() -> None:
    store = InMemoryPortfolioStore()
    degraded = DatabaseDegraded(store)
    service = PortfolioService(store, database_delay=degraded.delay_seconds)
    raju = (await service.open_account("RajuTrader", AccountKind.HUMAN))[0].trader_id

    await service.create_reservation(buy(1, raju, 1, 24600))
    await degraded.start(1500, NOW)
    await service.create_reservation(buy(2, raju, 1, 24600))
    assert store.state.database_waits == [1.5]  # only the reservation made while it was on

    restarted = DatabaseDegraded(store)  # a new pod (restart, rollback or another replica)
    assert restarted.delay_seconds() == 0.0
    await restarted.refresh()
    assert restarted.delay_seconds() == 1.5
    with pytest.raises(FaultAlreadyActiveError):
        await restarted.start(500, NOW)

    assert await restarted.stop()
    await degraded.refresh()
    await service.create_reservation(buy(3, raju, 1, 24600))
    assert store.state.database_waits == [1.5]
    assert not await restarted.stop()


async def test_the_watcher_picks_up_a_change_made_through_another_replica() -> None:
    store = InMemoryPortfolioStore()
    mine, other = DatabaseDegraded(store), DatabaseDegraded(store)
    watching = asyncio.create_task(mine.watch(interval=0.01))
    await other.start(800, NOW)
    for _ in range(100):
        if mine.delay_seconds():
            break
        await asyncio.sleep(0.01)
    assert mine.delay_seconds() == 0.8
    watching.cancel()


@pytest.fixture
def hooks(settings: Settings) -> Iterator[TestClient]:
    store = InMemoryPortfolioStore()
    service = PortfolioService(store)
    app = create_app(
        settings.model_copy(update={"chaos_hooks_enabled": True}),
        readiness_checks=[],
        service=service,
    )
    app.state.database_degraded = DatabaseDegraded(store)
    with TestClient(app) as client:
        yield client


def test_the_hooks_answer_404_unless_enabled(settings: Settings) -> None:
    with TestClient(create_app(settings, readiness_checks=[])) as client:
        assert client.post("/internal/v1/faults", json=DEGRADE).status_code == 404
        assert client.delete("/internal/v1/faults").status_code == 404


def test_before_the_database_is_reached_the_hooks_answer_503(settings: Settings) -> None:
    app = create_app(settings.model_copy(update={"chaos_hooks_enabled": True}), readiness_checks=[])
    with TestClient(app) as client:
        assert client.post("/internal/v1/faults", json=DEGRADE).status_code == 503


def test_inject_then_refuse_a_second_then_clear(hooks: TestClient) -> None:
    started = hooks.post("/internal/v1/faults", json=DEGRADE)
    assert started.status_code == 201
    body = started.json()
    assert (body["fault"], body["active"], body["delay_ms"]) == ("DB_DEGRADED", True, 1500)
    assert body["started_at"].endswith("Z")
    again = hooks.post("/internal/v1/faults", json=DEGRADE)
    assert (again.status_code, again.json()["code"]) == (409, "FAULT_ALREADY_ACTIVE")
    assert hooks.delete("/internal/v1/faults").status_code == 204
    gone = hooks.delete("/internal/v1/faults")
    assert (gone.status_code, gone.json()["code"]) == (404, "NO_ACTIVE_FAULT")


@pytest.mark.parametrize(
    "body",
    [
        {"fault": "DB_DEGRADED", "delay_ms": 99},
        {"fault": "DB_DEGRADED", "delay_ms": 5001},
        {"fault": "MEMORY_LEAK", "delay_ms": 1500},
        {"fault": "DB_DEGRADED"},
    ],
)
def test_a_bad_request_is_refused(hooks: TestClient, body: dict[str, object]) -> None:
    response = hooks.post("/internal/v1/faults", json=body)
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")
