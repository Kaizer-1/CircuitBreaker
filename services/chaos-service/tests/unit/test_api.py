"""HTTP: the status codes and Problem codes of api/chaos-service.yaml."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from chaos_service.application.lab import ChaosLab
from chaos_service.config import Settings
from chaos_service.main import create_app
from tests.fakes import FakeHooks, FakeOrderService

ADMIN = {"X-Role": "admin"}


@pytest.fixture
def world() -> tuple[ChaosLab, FakeOrderService, FakeHooks]:
    deployment, hooks = FakeOrderService(), FakeHooks()
    return ChaosLab(deployment, hooks), deployment, hooks


@pytest.fixture
def client(
    settings: Settings, world: tuple[ChaosLab, FakeOrderService, FakeHooks]
) -> Iterator[TestClient]:
    with TestClient(create_app(settings, readiness_checks=[], lab=world[0])) as c:
        yield c


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/chaos/scenarios"),
        ("POST", "/v1/chaos/experiments"),
        ("GET", "/v1/chaos/experiments"),
        ("GET", "/v1/chaos/experiments/x"),
        ("POST", "/v1/chaos/experiments/x/stop"),
        ("POST", "/v1/chaos/reset"),
    ],
)
def test_everything_is_for_the_admin_role_only(client: TestClient, method: str, path: str) -> None:
    response = client.request(
        method, path, json={"scenario": "BAD_DEPLOY"}, headers={"X-Role": "trader"}
    )
    assert (response.status_code, response.json()["code"]) == (403, "FORBIDDEN")


def test_the_four_scenarios_with_their_expected_fixes(client: TestClient) -> None:
    body = client.get("/v1/chaos/scenarios", headers=ADMIN).json()
    assert [(s["scenario"], s["expected_fix"]) for s in body["scenarios"]] == [
        ("BAD_DEPLOY", "rollback_deployment"),
        ("MEMORY_LEAK", "restart_deployment"),
        ("TRAFFIC_SPIKE", "scale_deployment"),
        ("PORTFOLIO_DB_DEGRADED", "do_nothing"),
    ]
    assert body["scenarios"][2]["default_parameters"] == {"requests_per_second": 110}


def test_start_overlap_stop_and_the_list(
    client: TestClient, world: tuple[ChaosLab, FakeOrderService, FakeHooks]
) -> None:
    started = client.post(
        "/v1/chaos/experiments",
        json={"scenario": "MEMORY_LEAK", "parameters": {"rate_mb_per_minute": 120}},
        headers=ADMIN,
    )
    assert started.status_code == 201
    leak = started.json()
    assert (leak["status"], leak["parameters"], leak["target_service"]) == (
        "RUNNING",
        {"rate_mb_per_minute": 120},
        "market-data-service",
    )
    assert client.post("/v1/chaos/experiments", json={"scenario": "BAD_DEPLOY"}, headers=ADMIN)
    refused = client.post(
        "/v1/chaos/experiments", json={"scenario": "TRAFFIC_SPIKE"}, headers=ADMIN
    )
    assert (refused.status_code, refused.json()["code"]) == (409, "EXPERIMENT_ALREADY_RUNNING")
    assert "already affects order-service" in refused.json()["detail"]

    stopped = client.post(f"/v1/chaos/experiments/{leak['experiment_id']}/stop", headers=ADMIN)
    assert (stopped.status_code, stopped.json()["status"]) == (200, "STOPPED")
    again = client.post(f"/v1/chaos/experiments/{leak['experiment_id']}/stop", headers=ADMIN)
    assert (again.status_code, again.json()["code"]) == (409, "EXPERIMENT_NOT_RUNNING")
    missing = client.post("/v1/chaos/experiments/nope/stop", headers=ADMIN)
    assert missing.status_code == 404

    listed = client.get("/v1/chaos/experiments", headers=ADMIN).json()
    assert [e["scenario"] for e in listed["items"]] == ["BAD_DEPLOY", "MEMORY_LEAK"]  # newest first
    one = client.get(f"/v1/chaos/experiments/{leak['experiment_id']}", headers=ADMIN).json()
    assert one["timeline"][-1]["message"] == "Stopped by the presenter. Leak cleared."


@pytest.mark.parametrize(
    "body",
    [
        {"scenario": "CPU_BURN"},
        {"scenario": "BAD_DEPLOY", "duration_seconds": 30},
        {"scenario": "BAD_DEPLOY", "parameters": {"error_rate": 0.95}},
        {"scenario": "BAD_DEPLOY", "parameters": {"delay_ms": 1500}},  # another scenario's
    ],
)
def test_a_bad_start_is_refused(client: TestClient, body: dict[str, object]) -> None:
    response = client.post("/v1/chaos/experiments", json=body, headers=ADMIN)
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")


def test_a_target_that_is_down_is_a_503_and_starts_nothing(
    client: TestClient, world: tuple[ChaosLab, FakeOrderService, FakeHooks]
) -> None:
    world[2].down = {"panic"}
    response = client.post(
        "/v1/chaos/experiments", json={"scenario": "TRAFFIC_SPIKE"}, headers=ADMIN
    )
    assert (response.status_code, response.json()["code"]) == (503, "DEPENDENCY_UNAVAILABLE")
    assert client.get("/v1/chaos/experiments", headers=ADMIN).json()["items"] == []


def test_reset_reports_what_it_undid_and_lists_it(
    client: TestClient, world: tuple[ChaosLab, FakeOrderService, FakeHooks]
) -> None:
    client.post("/v1/chaos/experiments", json={"scenario": "TRAFFIC_SPIKE"}, headers=ADMIN)
    world[1]._replicas = 5  # the agent scaled up
    reset = client.post("/v1/chaos/reset", headers=ADMIN)
    assert reset.status_code == 200
    assert reset.json()["undone"] == [
        "the panic sell stopped",
        "order-service back to 2 replicas (was 5)",
    ]
    listed = client.get("/v1/chaos/experiments", headers=ADMIN).json()
    assert listed["items"][0]["status"] == "STOPPED"
    assert listed["resets"][0]["undone"] == reset.json()["undone"]
