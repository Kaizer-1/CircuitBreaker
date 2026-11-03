"""Smoke tests for the runtime plumbing that every service shares (same file in every service)."""

import json
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from order_service.config import Settings
from order_service.main import create_app
from order_service.runtime.health import ReadinessCheck, health_router
from order_service.runtime.jsonlog import configure_logging
from order_service.runtime.metrics import Metrics, metrics_router
from order_service.runtime.middleware import ObservabilityMiddleware
from order_service.runtime.problem import install_exception_handlers


async def _ok() -> None:
    return None


async def _down() -> None:
    raise ConnectionError("dependency is down")


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings, readiness_checks=[ReadinessCheck("fake-ok", _ok)])


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def plain_app(settings: Settings) -> FastAPI:
    """Only the shared runtime pieces, with the pass-through correlation ID that most services use.

    (The gateway issues its own correlation IDs; its tests cover that.)
    """
    metrics = Metrics(settings.service_name)
    app = FastAPI()
    app.add_middleware(
        ObservabilityMiddleware,
        service=settings.service_name,
        metrics=metrics,
        issue_correlation_id=False,
    )
    install_exception_handlers(app)
    app.include_router(health_router(settings.service_name, []))
    app.include_router(metrics_router(metrics))
    return app


def test_healthz_is_ok_even_when_a_dependency_is_down(settings: Settings) -> None:
    app = create_app(settings, readiness_checks=[ReadinessCheck("fake-down", _down)])
    with TestClient(app) as c:
        response = c.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_reports_ready_when_every_check_passes(client: TestClient) -> None:
    response = client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["fake-ok"]["ok"] is True


def test_readyz_returns_503_with_details_when_a_check_fails(settings: Settings) -> None:
    app = create_app(
        settings,
        readiness_checks=[ReadinessCheck("fake-ok", _ok), ReadinessCheck("fake-down", _down)],
    )
    with TestClient(app) as c:
        response = c.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["fake-ok"]["ok"] is True
    assert body["checks"]["fake-down"]["ok"] is False
    assert "dependency is down" in body["checks"]["fake-down"]["error"]


def test_metrics_exposes_the_standard_metrics(client: TestClient) -> None:
    client.get("/healthz")
    text = client.get("/metrics").text
    assert "http_requests_total{" in text
    assert 'route="/healthz"' in text
    assert "http_request_duration_seconds_bucket" in text
    assert "http_requests_in_flight" in text
    for name in ("events_published_total", "events_consumed_total", "consumer_lag_messages"):
        assert name in text


def test_correlation_id_is_generated_when_missing(plain_app: FastAPI) -> None:
    with TestClient(plain_app) as client:
        response = client.get("/healthz")
    assert len(response.headers["X-Correlation-Id"]) >= 32


def test_a_valid_incoming_correlation_id_is_passed_through(plain_app: FastAPI) -> None:
    with TestClient(plain_app) as client:
        response = client.get("/healthz", headers={"X-Correlation-Id": "from-gateway-123"})
    assert response.headers["X-Correlation-Id"] == "from-gateway-123"


def test_a_malformed_incoming_correlation_id_is_replaced(plain_app: FastAPI) -> None:
    with TestClient(plain_app) as client:
        response = client.get("/healthz", headers={"X-Correlation-Id": 'bad"id with spaces'})
    assert response.headers["X-Correlation-Id"] != 'bad"id with spaces'


def test_unknown_path_is_a_problem_details_404(plain_app: FastAPI) -> None:
    with TestClient(plain_app) as client:
        response = client.get("/v1/does-not-exist", headers={"X-Correlation-Id": "cid-1"})
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["code"] == "NOT_FOUND"
    assert body["correlation_id"] == "cid-1"


def test_validation_errors_list_the_fields(plain_app: FastAPI) -> None:
    async def typed(limit: int) -> dict[str, int]:
        return {"limit": limit}

    plain_app.add_api_route("/test-only/typed", typed)
    with TestClient(plain_app) as c:
        response = c.get("/test-only/typed", params={"limit": "not-a-number"})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "VALIDATION_FAILED"
    assert body["errors"][0]["field"] == "limit"


def test_an_unhandled_exception_becomes_a_problem_details_500(plain_app: FastAPI) -> None:
    async def boom() -> None:
        raise RuntimeError("secret internals must not leak")

    plain_app.add_api_route("/test-only/boom", boom)
    with TestClient(plain_app, raise_server_exceptions=False) as c:
        response = c.get("/test-only/boom", headers={"X-Correlation-Id": "cid-500"})
    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "INTERNAL_ERROR"
    assert body["correlation_id"] == "cid-500"
    assert "secret internals" not in response.text
    assert response.headers["X-Correlation-Id"] == "cid-500"


def test_access_log_is_one_json_line_with_the_correlation_id(
    capsys: pytest.CaptureFixture[str], plain_app: FastAPI, settings: Settings
) -> None:
    configure_logging(settings.service_name, "INFO")  # binds the handler to the captured stdout
    with TestClient(plain_app) as c:
        c.get("/healthz", headers={"X-Correlation-Id": "log-test-1"})
    out = capsys.readouterr().out
    lines = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    entry = next(
        line for line in lines if line.get("message") == "request" and line["path"] == "/healthz"
    )
    assert entry["correlation_id"] == "log-test-1"
    assert entry["service"] == settings.service_name
    assert entry["status"] == 200
    assert entry["level"] == "INFO"
    assert "timestamp" in entry
