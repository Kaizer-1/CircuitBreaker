"""A-12/A-13: the memory-leak hook (chaos scenarios section 3.2)."""

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from market_data_service.application.faults import MIB, PAGE, FaultAlreadyActiveError, MemoryLeak
from market_data_service.config import Settings
from market_data_service.main import create_app

LEAK = {"fault": "MEMORY_LEAK", "rate_mb_per_minute": 80}


@pytest.fixture
def hooks(settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings.model_copy(update={"chaos_hooks_enabled": True}), readiness_checks=[])
    with TestClient(app) as client:
        yield client


async def test_the_leak_grows_at_its_rate_and_clearing_gives_everything_back() -> None:
    leak = MemoryLeak(tick_seconds=1.0)
    leak.start(80, datetime.now(UTC))
    for _ in range(60):  # one minute of ticks
        leak.grow()
    assert abs(leak.held_bytes - 80 * MIB) <= PAGE  # 80 MiB a minute, to the page
    with pytest.raises(FaultAlreadyActiveError):
        leak.start(40, datetime.now(UTC))
    assert await leak.stop()
    assert leak.held_bytes == 0
    assert not await leak.stop()


async def test_a_slow_leak_still_grows_steadily_in_whole_pages() -> None:
    leak = MemoryLeak(tick_seconds=1.0)
    leak.start(1, datetime.now(UTC))  # 1 MiB a minute: 17,476 bytes a second
    sizes = []
    for _ in range(10):
        leak.grow()
        sizes.append(leak.held_bytes)
    assert all(b > a for a, b in zip(sizes, sizes[1:], strict=False))
    assert all(size % PAGE == 0 for size in sizes)
    await leak.stop()


def test_the_hooks_answer_404_unless_enabled(settings: Settings) -> None:
    with TestClient(create_app(settings, readiness_checks=[])) as client:
        assert client.post("/internal/v1/faults", json=LEAK).status_code == 404
        assert client.delete("/internal/v1/faults").status_code == 404


def test_inject_then_refuse_a_second_then_clear(hooks: TestClient) -> None:
    started = hooks.post("/internal/v1/faults", json=LEAK)
    assert started.status_code == 201
    body = started.json()
    assert (body["fault"], body["active"], body["rate_mb_per_minute"]) == ("MEMORY_LEAK", True, 80)
    assert body["started_at"].endswith("Z")
    again = hooks.post("/internal/v1/faults", json=LEAK)
    assert (again.status_code, again.json()["code"]) == (409, "FAULT_ALREADY_ACTIVE")
    assert hooks.delete("/internal/v1/faults").status_code == 204
    gone = hooks.delete("/internal/v1/faults")
    assert (gone.status_code, gone.json()["code"]) == (404, "NO_ACTIVE_FAULT")


@pytest.mark.parametrize(
    "body",
    [
        {"fault": "MEMORY_LEAK", "rate_mb_per_minute": 0},
        {"fault": "MEMORY_LEAK", "rate_mb_per_minute": 201},
        {"fault": "CPU_BURN", "rate_mb_per_minute": 40},
        {"fault": "MEMORY_LEAK"},
    ],
)
def test_a_bad_request_is_refused(hooks: TestClient, body: dict[str, object]) -> None:
    response = hooks.post("/internal/v1/faults", json=body)
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")
