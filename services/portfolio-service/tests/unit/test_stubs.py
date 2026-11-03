"""Phase 1 stubs: every operation in the spec answers 501 with a Problem Details body."""

import re

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from portfolio_service.config import Settings
from portfolio_service.main import create_app


def test_every_stub_returns_501_problem_details(settings: Settings) -> None:
    app = create_app(settings, readiness_checks=[])
    stubs = [
        r
        for r in app.routes
        if isinstance(r, APIRoute) and r.endpoint.__module__.endswith(".api.stubs")
    ]
    with TestClient(app) as client:
        for route in stubs:
            path = re.sub(r"\{[^}]+\}", "x", route.path)
            for method in route.methods or ():
                response = client.request(method, path, headers={"X-Correlation-Id": "stub-test"})
                assert response.status_code == 501, (method, route.path)
                assert response.headers["content-type"].startswith("application/problem+json")
                body = response.json()
                assert body["code"] == "NOT_IMPLEMENTED"
                assert body["correlation_id"] == "stub-test"
