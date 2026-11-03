"""Gateway behaviour: proxying, the /internal/ block, gateway-owned stubs and the status report."""

from collections.abc import Callable

import httpx
from fastapi.testclient import TestClient

from api_gateway.config import Settings
from api_gateway.main import create_app


def _client(settings: Settings, handler: Callable[[httpx.Request], httpx.Response]) -> TestClient:
    app = create_app(settings, readiness_checks=[], http_transport=httpx.MockTransport(handler))
    return TestClient(app, raise_server_exceptions=False)


def test_proxy_forwards_to_the_owning_service_and_strips_identity_headers(
    settings: Settings,
) -> None:
    seen: dict[str, httpx.Request] = {}

    def upstream(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(501, json={"code": "NOT_IMPLEMENTED"}, headers={"X-Upstream": "yes"})

    with _client(settings, upstream) as client:
        response = client.post(
            "/v1/orders?dry_run=1",
            json={"ticker": "DABBA"},
            headers={"X-Trader-Id": "forged", "X-Role": "admin", "X-Correlation-Id": "client-set"},
        )

    request = seen["request"]
    assert str(request.url) == "http://127.0.0.1:9/v1/orders?dry_run=1"
    assert request.method == "POST"
    assert "x-trader-id" not in request.headers  # identity comes only from the verified JWT
    assert "x-role" not in request.headers
    assert response.status_code == 501
    assert response.headers["X-Upstream"] == "yes"
    # The gateway issues the correlation ID, ignores the client's, and forwards its own.
    assert request.headers["x-correlation-id"] == response.headers["X-Correlation-Id"]
    assert response.headers["X-Correlation-Id"] != "client-set"


def test_proxy_returns_503_when_the_upstream_is_down(settings: Settings) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with _client(settings, down) as client:
        response = client.get("/v1/orders")
    assert response.status_code == 503
    assert response.json()["code"] == "DEPENDENCY_UNAVAILABLE"


def test_operations_the_gateway_owns_are_501_stubs(settings: Settings) -> None:
    with _client(settings, lambda r: httpx.Response(200)) as client:
        response = client.post("/v1/sessions", json={})
    assert response.status_code == 501
    assert response.json()["code"] == "NOT_IMPLEMENTED"


def test_internal_paths_are_never_routed(settings: Settings) -> None:
    called: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        called.append(request)
        return httpx.Response(200)

    with _client(settings, upstream) as client:
        get = client.get("/internal/v1/events")
        post = client.post("/internal/v1/reservations", json={})
    assert get.status_code == post.status_code == 404
    assert get.json()["code"] == "NOT_FOUND"
    assert called == []


def _readyz_handler(request: httpx.Request) -> httpx.Response:
    ok = {"ok": True, "latency_ms": 1.0}
    if request.url.host == "order-service":
        return httpx.Response(
            200, json={"status": "ready", "checks": {"redpanda": ok, "cassandra": ok}}
        )
    if request.url.host == "matching-engine":
        down = {"ok": False, "latency_ms": 3000.0, "error": "ConnectionError: down"}
        return httpx.Response(
            503, json={"status": "not_ready", "checks": {"redpanda": ok, "cassandra": down}}
        )
    raise httpx.ConnectError("refused")


def test_system_status_combines_every_service_and_groups_dependencies() -> None:
    settings = Settings(
        service_upstreams={
            "order-service": "http://order-service:8000",
            "matching-engine": "http://matching-engine:8000",
            "sre-agent": "http://sre-agent:8000",
        }
    )
    with _client(settings, _readyz_handler) as client:
        response = client.get("/v1/system/status")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unhealthy"
    by_name = {s["name"]: s for s in body["services"]}
    assert by_name["api-gateway"]["status"] == "ready"
    assert by_name["order-service"]["status"] == "ready"
    assert by_name["matching-engine"]["status"] == "not_ready"
    assert by_name["sre-agent"]["status"] == "unreachable"
    deps = {d["name"]: d["status"] for d in body["dependencies"]}
    assert deps == {"redpanda": "ok", "cassandra": "degraded"}


def test_system_status_is_healthy_when_every_service_is_ready() -> None:
    settings = Settings(service_upstreams={"order-service": "http://order-service:8000"})
    with _client(settings, _readyz_handler) as client:
        body = client.get("/v1/system/status").json()
    assert body["status"] == "healthy"
    assert body["checked_at"].endswith("Z")
