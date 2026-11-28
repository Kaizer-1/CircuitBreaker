"""The HTTP API and the feed (Q-17..Q-20, A-14, A-15) over a test agent and an in-memory store."""

import asyncio
from datetime import timedelta
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from sre_agent.config import Settings
from sre_agent.domain.actions import Proposal, Tool
from sre_agent.domain.incident import Incident, Resolution
from sre_agent.main import create_app
from tests.fakes import Clock, World
from tests.unit.test_agent import Rig

ADMIN = {"X-Role": "admin", "X-Nickname": "presenter"}
TRADER = {"X-Role": "trader", "X-Nickname": "RajuTrader"}


def settings() -> Settings:
    return Settings(cassandra_password=SecretStr("unused"), environment="staging")


def app_with_incidents() -> tuple[FastAPI, Rig]:
    rig = Rig(World(Clock()))
    for n in range(1, 4):
        i, _ = Incident.open(n, "OrderServiceHighLatency", "order-service", "critical", f"#{n}",
                             rig.clock.now() + timedelta(minutes=n))  # fmt: skip
        if n < 3:
            i.resolve(Resolution.AUTO_CLEARED, "Cleared.", rig.clock.now() + timedelta(minutes=n))
        asyncio.run(rig.store.save(i))
    return create_app(settings(), [], agent=rig.agent, store=rig.store), rig


def test_incidents_are_listed_newest_first_filtered_and_paged() -> None:
    app, _ = app_with_incidents()
    with TestClient(app) as c:
        body = c.get("/v1/incidents", headers=TRADER).json()
        assert [i["incident_id"] for i in body["items"]] == ["INC-0003", "INC-0002", "INC-0001"]
        assert body["next_cursor"] is None
        resolved = c.get("/v1/incidents?status=RESOLVED", headers=TRADER).json()["items"]
        assert [i["incident_id"] for i in resolved] == ["INC-0002", "INC-0001"]
        first = c.get("/v1/incidents?limit=2", headers=TRADER).json()
        rest = c.get(f"/v1/incidents?limit=2&cursor={first['next_cursor']}", headers=TRADER).json()
        assert [i["incident_id"] for i in rest["items"]] == ["INC-0001"]
        assert c.get("/v1/incidents?cursor=yesterday", headers=TRADER).status_code == 400
        one = c.get("/v1/incidents/INC-0002", headers=TRADER).json()
        assert one["timeline"][0]["kind"] == "OPENED" and one["pending_remediation"] is None
        assert c.get("/v1/incidents/INC-0404", headers=TRADER).status_code == 404


def test_decisions_and_settings_are_for_admins_and_check_what_is_pending() -> None:
    app, rig = app_with_incidents()
    rid = "d4e5f6a7-b8c9-4d0e-8f1a-2b3c4d5e6f7a"
    with TestClient(app) as c:
        body = {"remediation_id": rid, "decision": "APPROVE"}
        assert (
            c.post("/v1/incidents/INC-0003/decision", json=body, headers=TRADER).status_code == 403
        )
        assert (
            c.post("/v1/incidents/INC-0404/decision", json=body, headers=ADMIN).status_code == 404
        )
        r = c.post("/v1/incidents/INC-0003/decision", json=body, headers=ADMIN)
        assert (r.status_code, r.json()["code"]) == (409, "NO_PENDING_REMEDIATION")
        bad = {**body, "decision": "MAYBE"}
        assert c.post("/v1/incidents/INC-0003/decision", json=bad, headers=ADMIN).status_code == 422
        assert c.get("/v1/agent/settings", headers=TRADER).status_code == 403
        s = c.get("/v1/agent/settings", headers=ADMIN).json()
        assert (s["environment"], s["approval_required"], s["paused"]) == ("staging", False, False)
        assert s["model"] == "none (rules only)"
        assert s["budget"] == {"actions_left_this_hour": 6, "cooldown_seconds_by_service": {}}
        assert c.put("/v1/agent/settings", json={}, headers=ADMIN).status_code == 422
        assert c.put("/v1/agent/settings", json={"model": "x"}, headers=ADMIN).status_code == 422
        changed = c.put("/v1/agent/settings", json={"paused": True}, headers=ADMIN).json()
        assert changed["paused"] is True and changed["updated_by"] == "presenter"
    assert rig.agent.settings.paused and rig.store.saved_settings["staging"].paused


def test_the_feed_starts_with_a_snapshot() -> None:
    app, _ = app_with_incidents()
    with TestClient(app) as c, c.websocket_connect("/v1/ws/incidents") as ws:
        first: dict[str, Any] = ws.receive_json()
        assert first["type"] == "snapshot"
        assert [i["incident_id"] for i in first["data"]] == ["INC-0002", "INC-0001"]
        second: dict[str, Any] = ws.receive_json()  # who is with a person, before any change
        assert second == {"type": "with_a_person", "data": {"incident_ids": []}}


def test_while_starting_the_api_answers_503_and_readiness_says_so() -> None:
    async def never(app: FastAPI, s: Settings, http: httpx.AsyncClient) -> None:
        await asyncio.Event().wait()

    app = create_app(settings(), None, start=never)
    with TestClient(app) as c:
        r = c.get("/v1/incidents", headers=TRADER)
        assert (r.status_code, r.json()["code"]) == (503, "DEPENDENCY_UNAVAILABLE")
        ready = c.get("/readyz")
        assert ready.status_code == 503 and "starting" in ready.text


def test_there_is_no_route_to_change_the_model_or_the_tools() -> None:
    app, _ = app_with_incidents()
    paths = {getattr(r, "path", "") for r in app.routes}
    assert not any("model" in p or "tool" in p for p in paths)
    assert Proposal(Tool.NOTHING.value, None, None, "x", "s").summary == "do_nothing"
