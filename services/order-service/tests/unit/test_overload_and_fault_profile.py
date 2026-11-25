"""The 50-in-flight limit (503 OVERLOADED) and chaos's bad release (FAULT_PROFILE=bad-release)."""

import asyncio
import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from order_service.application.fault_profile import FaultProfile
from order_service.config import Settings
from order_service.main import create_app
from tests.unit.application.test_orders import World
from tests.unit.test_api import LIMIT_BUY, ME


async def test_beyond_the_limit_a_request_is_turned_away_at_once_but_probes_are_not(
    settings: Settings,
) -> None:
    world = World()
    entered, gate = asyncio.Event(), asyncio.Event()
    real_place = world.orders.place

    async def held_place(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await gate.wait()
        return await real_place(*args, **kwargs)

    world.orders.place = held_place  # type: ignore[method-assign]
    app = create_app(
        settings.model_copy(update={"max_in_flight": 1}), readiness_checks=[], orders=world.orders
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://order-service") as client:
        first = asyncio.create_task(client.post("/v1/orders", json=LIMIT_BUY, headers=ME))
        await entered.wait()

        turned_away = await client.post("/v1/orders", json=LIMIT_BUY, headers=ME)
        assert (turned_away.status_code, turned_away.json()["code"]) == (503, "OVERLOADED")
        assert turned_away.headers["Retry-After"] == "1"
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.get("/metrics")).status_code == 200

        gate.set()
        assert (await first).status_code == 202
        assert (await client.post("/v1/orders", json=LIMIT_BUY, headers=ME)).status_code == 202
        metrics = (await client.get("/metrics")).text
    [counted] = [line for line in metrics.splitlines() if line.startswith("http_problems_total{")]
    assert 'code="OVERLOADED"' in counted and 'route="/v1/orders"' in counted
    assert counted.endswith(" 1.0")


def test_the_bad_release_fails_a_share_of_placements_with_an_unhandled_error(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World()
    app = create_app(settings, readiness_checks=[], orders=world.orders)
    draws = iter([0.1, 0.5])  # below 0.4 fails, above passes
    app.state.fault_profile = FaultProfile("bad-release", 0.4, draw=lambda: next(draws))
    with TestClient(app, raise_server_exceptions=False) as client:
        failed = client.post("/v1/orders", json=LIMIT_BUY, headers=ME)
        assert world.portfolio.calls == []  # failed before anything was reserved
        placed = client.post("/v1/orders", json=LIMIT_BUY, headers=ME)
    assert (failed.status_code, failed.json()["code"]) == (500, "INTERNAL_ERROR")
    assert "price_band" not in failed.text  # the body never shows internals
    lines = capsys.readouterr().out.splitlines()
    errors = [json.loads(line) for line in lines if '"ERROR"' in line]
    assert [e["exception"].splitlines()[-1] for e in errors] == [
        "ValueError: price_band not configured"
    ]
    assert placed.status_code == 202


def test_a_normal_release_never_fails_and_only_known_profiles_are_accepted(
    settings: Settings,
) -> None:
    FaultProfile("", 0.9, draw=lambda: 0.0).before_place()
    with pytest.raises(ValueError, match="price_band"):
        FaultProfile("bad-release", 0.4, draw=lambda: 0.39).before_place()
    FaultProfile("bad-release", 0.4, draw=lambda: 0.4).before_place()
    with pytest.raises(ValidationError):
        Settings(**settings.model_dump() | {"fault_profile": "worse-release"})
