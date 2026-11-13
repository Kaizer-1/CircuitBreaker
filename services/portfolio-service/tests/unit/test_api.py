"""HTTP: Q-09, A-02 and A-06 with the status and Problem codes of api/portfolio-service.yaml."""

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_service.application.portfolio import PortfolioService
from portfolio_service.config import Settings
from portfolio_service.main import create_app
from tests.builders import oid
from tests.fakes import InMemoryPortfolioStore


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    service = PortfolioService(InMemoryPortfolioStore())
    asyncio.run(service.seed_accounts())
    with TestClient(create_app(settings, readiness_checks=[], service=service)) as c:
        yield c


def join(client: TestClient, nickname: str = "RajuTrader") -> str:
    response = client.post("/internal/v1/accounts", json={"nickname": nickname, "kind": "HUMAN"})
    assert response.status_code == 201
    return str(response.json()["trader_id"])


def reserve(client: TestClient, trader: str, **overrides: object) -> Any:
    body = {
        "order_id": oid(1),
        "trader_id": trader,
        "ticker": "CALVERA",
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": 50,
        "limit_price_paise": 24600,
    } | overrides
    return client.post("/internal/v1/reservations", json=body)


def test_a2_opens_a_human_account_once(client: TestClient) -> None:
    response = client.post(
        "/internal/v1/accounts", json={"nickname": "RajuTrader", "kind": "HUMAN"}
    )
    assert response.status_code == 201
    body = response.json()
    assert (body["nickname"], body["kind"], body["cash_paise"]) == (
        "RajuTrader",
        "HUMAN",
        10_000_000,
    )
    again = client.post("/internal/v1/accounts", json={"nickname": "RajuTrader", "kind": "HUMAN"})
    assert (again.status_code, again.json()["code"]) == (409, "NICKNAME_TAKEN")


def test_a2_returns_a_seeded_bot_with_200(client: TestClient) -> None:
    response = client.post("/internal/v1/accounts", json={"nickname": "bot-07", "kind": "BOT"})
    assert response.status_code == 200
    assert response.json()["trader_id"] == "b0b0b0b0-0007-4000-8000-000000000007"


def test_a2_validates_the_nickname(client: TestClient) -> None:
    response = client.post("/internal/v1/accounts", json={"nickname": "x", "kind": "HUMAN"})
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")


def test_a6_reserves_with_201_and_repeats_with_200(client: TestClient) -> None:
    raju = join(client)
    first = reserve(client, raju)
    assert first.status_code == 201
    body = first.json()
    assert (body["status"], body["reserved_cash_paise"], body["protection_price_paise"]) == (
        "ACTIVE",
        1_230_000,
        None,
    )
    again = reserve(client, raju)
    assert again.status_code == 200
    assert again.json()["reservation_id"] == body["reservation_id"]


def test_a6_refuses_margin_and_short_selling(client: TestClient) -> None:
    raju = join(client)
    no_cash = reserve(client, raju, quantity=10_000)
    assert (no_cash.status_code, no_cash.json()["code"]) == (422, "INSUFFICIENT_FUNDS")
    no_shares = reserve(client, raju, order_id=oid(2), side="SELL")
    assert (no_shares.status_code, no_shares.json()["code"]) == (422, "INSUFFICIENT_SHARES")


def test_a6_market_orders_get_a_protection_price(client: TestClient) -> None:
    raju = join(client)
    body = reserve(
        client, raju, order_type="MARKET", limit_price_paise=None, ticker="HELIOR", quantity=100
    ).json()
    assert (body["protection_price_paise"], body["reserved_cash_paise"]) == (9705, 970_500)


def test_a6_validation_errors(client: TestClient) -> None:
    raju = join(client)
    off_tick = reserve(client, raju, limit_price_paise=24603)
    assert off_tick.status_code == 422
    no_price = reserve(client, raju, limit_price_paise=None)
    assert (no_price.status_code, no_price.json()["code"]) == (422, "VALIDATION_FAILED")
    stranger = reserve(client, "00000000-0000-4000-8000-000000000000")
    assert (stranger.status_code, stranger.json()["code"]) == (404, "NOT_FOUND")


def test_q9_returns_my_portfolio_by_the_gateways_trader_header(client: TestClient) -> None:
    raju = join(client)
    reserve(client, raju)
    body = client.get("/v1/portfolio", headers={"X-Trader-Id": raju}).json()
    assert (body["available_cash_paise"], body["reserved_cash_paise"]) == (8_770_000, 1_230_000)
    unknown = client.get(
        "/v1/portfolio", headers={"X-Trader-Id": "00000000-0000-4000-8000-000000000000"}
    )
    assert unknown.status_code == 404
    assert (
        client.get("/v1/portfolio").status_code == 422
    )  # only the gateway calls it, with the header


def test_while_the_database_is_not_ready_the_api_says_503(settings: Settings) -> None:
    with TestClient(create_app(settings, readiness_checks=[])) as c:
        response = c.post("/internal/v1/accounts", json={"nickname": "RajuTrader", "kind": "HUMAN"})
        assert (response.status_code, response.json()["code"]) == (503, "DEPENDENCY_UNAVAILABLE")
        assert c.get("/v1/portfolio", headers={"X-Trader-Id": oid(1)}).status_code == 503
        assert reserve(c, oid(2)).status_code == 503
