"""The store on a real Cassandra: incidents round-trip, newest first across days, settings and
the action log survive, and incident numbers are never given out twice."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from sre_agent.adapters.cassandra_store import CassandraStore
from sre_agent.domain.actions import Proposal, Tool
from sre_agent.domain.incident import Incident, Resolution
from sre_agent.ports.store import ActionRecord, AgentSettings

pytestmark = pytest.mark.integration
T0 = datetime(2026, 11, 26, 0, 30, tzinfo=UTC)


async def test_an_incident_round_trips_with_its_remediations_and_timeline(
    store: CassandraStore,
) -> None:
    fired = T0 - timedelta(seconds=5)
    incident, _ = Incident.open(
        7, "OrderServiceHighErrorRate", "order-service", "critical", "errors", T0, fired_at=fired
    )
    p = Proposal(Tool.ROLLBACK.value, "order-service", None, "Bad release.", "anthropic/x")
    r, _ = incident.propose(p, p.explanation, {"to_revision": 8}, False, T0)
    git = {"status": "NOT_NEEDED", "reverted_commit": None, "pull_request_url": None,
           "reason": "drift"}  # fmt: skip
    incident.executed(r.remediation_id, True, "Rolled back.", T0 + timedelta(seconds=9), git)
    incident.recovered_at = T0 + timedelta(seconds=40)
    incident.resolve(Resolution.RECOVERED, "Recovered.", T0 + timedelta(seconds=100))
    await store.save(incident)
    back = await store.get("INC-0007")
    assert back is not None and back.to_api() == incident.to_api()
    assert back.remediations[0].details == "Rolled back."
    assert await store.get("INC-9999") is None


async def test_recent_lists_newest_first_across_days(store: CassandraStore) -> None:
    for n, minutes in enumerate([10, 50, 70, 24 * 60 + 5], start=1):  # the last one: two days ago
        i, _ = Incident.open(
            n, "A", "order-service", "warning", "s", T0 - timedelta(minutes=minutes)
        )
        await store.save(i)
    assert [i.incident_id for i in await store.recent(10, T0)] == [
        "INC-0001", "INC-0002", "INC-0003", "INC-0004"]  # fmt: skip
    assert [i.incident_id for i in await store.recent(2, T0)] == ["INC-0001", "INC-0002"]


async def test_settings_and_the_action_log(store: CassandraStore) -> None:
    assert await store.settings("staging") is None
    s = AgentSettings("staging", True, False, T0, "presenter")
    await store.save_settings(s)
    assert await store.settings("staging") == s
    await store.record_action(ActionRecord("order-service", T0, "INC-0001", "scale_deployment"))
    await store.record_action(
        ActionRecord("market-data-service", T0 - timedelta(hours=2), "INC-0002", "restart")
    )
    recent = await store.actions_since(T0 - timedelta(hours=1))
    assert [(a.service, a.action) for a in recent] == [("order-service", "scale_deployment")]


async def test_incident_numbers_are_unique_even_when_taken_at_once(store: CassandraStore) -> None:
    numbers = await asyncio.gather(*(store.next_incident_number() for _ in range(8)))
    assert sorted(numbers) == list(range(1, 9))
