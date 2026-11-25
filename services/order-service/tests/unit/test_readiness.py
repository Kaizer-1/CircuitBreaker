"""order-service's readiness (coding standards section 7, amended 2026-11-25): ready once the order
store and the publisher are up, without waiting for its matching.v1 consumer. No answer to a request
depends on this pod's consumer, and a scaled-up replica must take requests at once."""

import pytest
from fastapi.testclient import TestClient

from order_service import main
from order_service.config import Settings
from order_service.main import create_app
from tests.unit.application.test_orders import World


def test_ready_once_started_without_waiting_for_its_consumer(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "default_checks", lambda _: [])  # no real Cassandra or Redpanda here
    app = create_app(settings)
    with TestClient(app) as client:
        starting = client.get("/readyz")
        assert starting.status_code == 503  # the order store and publisher are not up yet
        assert set(starting.json()["checks"]) == {"started"}

        app.state.orders = World().orders  # started: the store migrated, the publisher connected
        assert app.state.consumer is None  # and no consumer at all, not even joined
        ready = client.get("/readyz")
        assert ready.status_code == 200, ready.json()
        assert set(ready.json()["checks"]) == {"started"}
