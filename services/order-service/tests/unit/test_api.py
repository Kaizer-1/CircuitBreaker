"""HTTP: the status codes and Problem codes of api/order-service.yaml."""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from order_service.config import Settings
from order_service.main import create_app
from tests.unit.application.test_orders import MEERA, RAJU, World

ME = {"X-Trader-Id": RAJU}
LIMIT_BUY = {
    "ticker": "CHAIWALA",
    "side": "BUY",
    "order_type": "LIMIT",
    "quantity": 50,
    "limit_price_paise": 24600,
}


@pytest.fixture
def world() -> World:
    return World()


@pytest.fixture
def client(settings: Settings, world: World) -> Iterator[TestClient]:
    with TestClient(create_app(settings, readiness_checks=[], orders=world.orders)) as c:
        yield c


def place(
    client: TestClient, body: dict[str, Any] | None = None, headers: dict[str, str] | None = None
) -> Any:
    return client.post("/v1/orders", json=body or LIMIT_BUY, headers=ME | (headers or {}))


def test_placing_an_order_answers_202_with_the_order_and_its_location(client: TestClient) -> None:
    response = place(client)
    assert response.status_code == 202
    body = response.json()
    assert response.headers["Location"] == f"/v1/orders/{body['order_id']}"
    assert {
        k: body[k] for k in ("status", "remaining_quantity", "cancel_requested", "rejection_reason")
    } == {
        "status": "PENDING",
        "remaining_quantity": 50,
        "cancel_requested": False,
        "rejection_reason": None,
    }


@pytest.mark.parametrize(
    ("changes", "code", "field"),
    [
        ({"ticker": "TATA"}, "UNKNOWN_TICKER", "ticker"),
        ({"limit_price_paise": 24603}, "INVALID_TICK_SIZE", "limit_price_paise"),
        ({"quantity": 0}, "VALIDATION_FAILED", "quantity"),
        ({"order_type": "MARKET"}, "VALIDATION_FAILED", "limit_price_paise"),
    ],
)
def test_invalid_orders_answer_422_with_a_specific_code(
    client: TestClient, changes: dict[str, Any], code: str, field: str
) -> None:
    response = place(client, LIMIT_BUY | changes)
    assert (response.status_code, response.json()["code"]) == (422, code)
    assert response.json()["errors"][0]["field"] == field


def test_malformed_requests(client: TestClient) -> None:
    bad_json = client.post(
        "/v1/orders", content=b"{", headers=ME | {"content-type": "application/json"}
    )
    assert (bad_json.status_code, bad_json.json()["code"]) == (400, "BAD_REQUEST")
    extra = place(client, LIMIT_BUY | {"margin": True})
    assert (extra.status_code, extra.json()["code"]) == (422, "VALIDATION_FAILED")
    no_trader = client.post("/v1/orders", json=LIMIT_BUY)
    assert no_trader.status_code == 422  # only the gateway calls this, always with X-Trader-Id


def test_not_enough_cash_is_422_insufficient_funds(client: TestClient) -> None:
    response = place(client, LIMIT_BUY | {"quantity": 10_000})
    assert (response.status_code, response.json()["code"]) == (422, "INSUFFICIENT_FUNDS")


def test_portfolio_down_is_503_dependency_unavailable(client: TestClient, world: World) -> None:
    world.portfolio.down = True
    response = place(client)
    assert (response.status_code, response.json()["code"]) == (503, "DEPENDENCY_UNAVAILABLE")


def test_the_idempotency_key_header_makes_retries_safe(client: TestClient) -> None:
    first = place(client, headers={"Idempotency-Key": "9b1f0c3e-phone-retry-1"})
    again = place(client, headers={"Idempotency-Key": "9b1f0c3e-phone-retry-1"})
    assert first.json()["order_id"] == again.json()["order_id"]


def test_get_list_and_cancel(client: TestClient) -> None:
    order_id = place(client).json()["order_id"]
    assert client.get(f"/v1/orders/{order_id}", headers=ME).json()["order_id"] == order_id
    assert client.get(f"/v1/orders/{order_id}", headers={"X-Trader-Id": MEERA}).status_code == 404
    listed = client.get("/v1/orders", params={"status": "open"}, headers=ME).json()
    assert [o["order_id"] for o in listed["items"]] == [order_id] and listed["next_cursor"] is None
    cancelled = client.delete(f"/v1/orders/{order_id}", headers=ME)
    assert (cancelled.status_code, cancelled.json()["cancel_requested"]) == (202, True)
    again = client.delete(f"/v1/orders/{order_id}", headers=ME)
    assert (again.status_code, again.json()["code"]) == (409, "ORDER_NOT_CANCELLABLE")
    assert (
        client.delete(f"/v1/orders/{order_id}", headers={"X-Trader-Id": MEERA}).status_code == 404
    )


def test_listing_validates_its_parameters(client: TestClient) -> None:
    assert client.get("/v1/orders", params={"status": "SOMETIMES"}, headers=ME).status_code == 422
    assert client.get("/v1/orders", params={"limit": 101}, headers=ME).status_code == 422
    bad = client.get("/v1/orders", params={"cursor": "%%%"}, headers=ME)
    assert (bad.status_code, bad.json()["code"]) == (400, "BAD_REQUEST")


def test_a19_accepts_an_expiry_request_with_202(client: TestClient) -> None:
    body = {
        "order_id": "6c7d8e9f-0a1b-4c2d-8e3f-4a5b6c7d8e9f",
        "trader_id": RAJU,
        "ticker": "CHAIWALA",
    }
    response = client.post("/internal/v1/order-expiries", json=body)
    assert response.status_code == 202
    assert response.json()["known_order"] is False
    again = client.post("/internal/v1/order-expiries", json=body)
    assert again.json()["requested_at"] == response.json()["requested_at"]


def test_while_the_store_is_not_ready_every_operation_says_503(settings: Settings) -> None:
    with TestClient(create_app(settings, readiness_checks=[])) as c:
        assert place(c).status_code == 503
        assert c.get("/v1/orders", headers=ME).status_code == 503
        assert c.get(f"/v1/orders/{RAJU}", headers=ME).status_code == 503
        assert c.delete(f"/v1/orders/{RAJU}", headers=ME).status_code == 503
        body = {"order_id": RAJU, "trader_id": RAJU, "ticker": "CHAIWALA"}
        assert c.post("/internal/v1/order-expiries", json=body).status_code == 503
