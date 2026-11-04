"""A-06 over HTTP through the circuit breaker, and the breaker's Prometheus metrics."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from prometheus_client import generate_latest

from order_service.adapters.portfolio_http import HttpPortfolio
from order_service.application.circuit_breaker import BreakerState, CircuitBreaker
from order_service.business_metrics import register_business_metrics
from order_service.config import Settings
from order_service.domain.order import OrderRequest, OrderType, RejectionReason, Side
from order_service.main import create_app
from order_service.ports.portfolio import DependencyUnavailableError, ReservationRefusedError
from order_service.runtime.metrics import Metrics

REQUEST = OrderRequest("CHAIWALA", Side.BUY, OrderType.LIMIT, 50, 24600)
ORDER, RAJU = "7f3c9e10-2b4d-4e6f-8a1b-3c5d7e9f0a12", "3d2a1b0c-9e8f-4a7b-8c6d-5e4f3a2b1c0d"


def client(handler: httpx.MockTransport) -> tuple[HttpPortfolio, CircuitBreaker, Metrics]:
    metrics = Metrics("order-service")
    business = register_business_metrics(metrics.registry)
    breaker = CircuitBreaker(
        on_state_change=lambda s: business.breaker_state("portfolio-service", s),
        on_trip=business.circuit_breaker_trips_total.labels("portfolio-service").inc,
    )
    http = httpx.AsyncClient(transport=handler)
    return HttpPortfolio("http://portfolio", http, breaker), breaker, metrics


def answer(status: int, body: dict[str, object]) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(status, json=body))


async def test_a_reservation_returns_its_id_and_protection_price() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(201, json={"reservation_id": "r-1", "protection_price_paise": None})

    portfolio, breaker, _ = client(httpx.MockTransport(handler))
    result = await portfolio.reserve(ORDER, RAJU, REQUEST)
    assert (result.reservation_id, result.protection_price_paise) == ("r-1", None)
    assert seen[0] == {
        "order_id": ORDER,
        "trader_id": RAJU,
        "ticker": "CHAIWALA",
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": 50,
        "limit_price_paise": 24600,
    }
    assert breaker.state is BreakerState.CLOSED


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("INSUFFICIENT_FUNDS", RejectionReason.INSUFFICIENT_FUNDS),
        ("INSUFFICIENT_SHARES", RejectionReason.INSUFFICIENT_SHARES),
        ("VALIDATION_FAILED", RejectionReason.VALIDATION_FAILED),
    ],
)
async def test_a_refusal_is_an_answer_not_a_failure(code: str, reason: RejectionReason) -> None:
    portfolio, breaker, _ = client(answer(422, {"code": code, "detail": "no"}))
    for _ in range(6):
        with pytest.raises(ReservationRefusedError) as refused:
            await portfolio.reserve(ORDER, RAJU, REQUEST)
        assert refused.value.reason is reason
    assert breaker.state is BreakerState.CLOSED  # 422 never trips the breaker


async def test_five_failures_trip_the_breaker_and_the_metrics_show_it() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("portfolio-service did not answer")

    portfolio, breaker, metrics = client(httpx.MockTransport(down))
    for _ in range(5):
        with pytest.raises(DependencyUnavailableError, match="did not answer"):
            await portfolio.reserve(ORDER, RAJU, REQUEST)
    with pytest.raises(DependencyUnavailableError, match="circuit breaker is open"):
        await portfolio.reserve(ORDER, RAJU, REQUEST)  # fails fast, no call
    text = generate_latest(metrics.registry).decode()
    assert 'circuit_breaker_state{dependency="portfolio-service",state="open"} 1.0' in text
    assert 'circuit_breaker_state{dependency="portfolio-service",state="closed"} 0.0' in text
    assert 'circuit_breaker_trips_total{dependency="portfolio-service"} 1.0' in text
    assert breaker.trips == 1


async def test_a_5xx_counts_as_a_failure() -> None:
    portfolio, breaker, _ = client(answer(503, {"code": "DEPENDENCY_UNAVAILABLE"}))
    for _ in range(5):
        with pytest.raises(DependencyUnavailableError):
            await portfolio.reserve(ORDER, RAJU, REQUEST)
    assert breaker.state is BreakerState.OPEN


def test_the_breaker_metrics_are_on_metrics_from_the_start(settings: Settings) -> None:
    with TestClient(create_app(settings, readiness_checks=[])) as c:
        text = c.get("/metrics").text
    assert 'circuit_breaker_state{dependency="portfolio-service",state="closed"} 1.0' in text
    assert "circuit_breaker_trips_total" in text
