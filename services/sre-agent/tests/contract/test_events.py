"""Event contract tests: what the agent publishes on incidents.v1 must match
contracts/events/*.schema.json (generated from the event catalog)."""

import json
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from sre_agent.domain.actions import Proposal, Tool
from sre_agent.domain.incident import Event, Incident, Resolution
from sre_agent.runtime.events import build_event

REPO_ROOT = Path(__file__).resolve().parents[4]
CONTRACTS = REPO_ROOT / "contracts" / "events"
PRODUCED = {
    "IncidentOpened",
    "RemediationProposed",
    "RemediationApproved",
    "RemediationDeclined",
    "RemediationExecuted",
    "IncidentResolved",
}
T0 = datetime(2026, 11, 26, 11, 5, 31, tzinfo=UTC)


@cache
def validators() -> dict[str, Draft202012Validator]:
    schemas = [json.loads(p.read_text()) for p in CONTRACTS.glob("*.schema.json")]
    registry: Registry[Any] = Registry().with_resources(
        (s["$id"], Resource.from_contents(s)) for s in schemas
    )
    return {
        s["title"]: Draft202012Validator(s, registry=registry, format_checker=FormatChecker())
        for s in schemas
        if "x-topic" in s
    }


def assert_valid(events: list[Event]) -> None:
    for event_type, data in events:
        envelope = build_event(
            event_type,
            data,
            producer="sre-agent",
            environment="staging",
            correlation_id=data["incident_id"],
        )
        errors = [
            f"{list(e.absolute_path)}: {e.message}"
            for e in validators()[event_type].iter_errors(json.loads(envelope.to_json()))
        ]
        assert errors == [], f"{event_type} breaks its contract: {errors}"


def test_every_event_type_the_agent_produces_has_a_schema_on_incidents_v1() -> None:
    for event_type in PRODUCED:
        schema = validators()[event_type].schema
        assert (schema["x-topic"], schema["x-producer"]) == ("incidents.v1", "sre-agent")


def test_a_whole_incident_with_approval_rollback_and_git_step_meets_the_contracts() -> None:
    incident, events = Incident.open(7, "OrderServiceHighErrorRate", "order-service", "critical",
                                     "order-service errors at 38%", T0)  # fmt: skip
    p = Proposal(Tool.ROLLBACK.value, "order-service", None, "A bad release.", "llm:test")
    r, more = incident.propose(p, p.explanation, {"to_revision": 7}, True, T0)
    events += more
    events += incident.approve(r.remediation_id, "presenter", "Agreed.", T0 + timedelta(seconds=9))
    git = {"status": "NOT_NEEDED", "reverted_commit": None, "pull_request_url": None,
           "reason": "The bad revision did not come from Git."}  # fmt: skip
    events += incident.executed(r.remediation_id, True, "Rolled back.", T0, git)
    events += incident.resolve(Resolution.RECOVERED, "Resolved.", T0 + timedelta(seconds=94))
    assert [e[0] for e in events] == [
        "IncidentOpened",
        "RemediationProposed",
        "RemediationApproved",
        "RemediationExecuted",
        "IncidentResolved",
    ]
    assert_valid(events)


def test_declines_do_nothing_and_escalation_meet_the_contracts() -> None:
    incident, events = Incident.open(8, "OrderServiceCircuitOpen", "order-service", "critical",
                                     "breaker open", T0)  # fmt: skip
    bad = Proposal(Tool.SCALE.value, "cassandra", 9, "Scale the database.", "llm:test")
    _, more = incident.decline_proposal(bad, bad.explanation, "Not on the allowlist.", T0)
    events += more
    nothing = Proposal(Tool.NOTHING.value, None, None, "The cause is elsewhere.", "fixed rule")
    r, more = incident.propose(nothing, nothing.explanation, {}, False, T0)
    events += more + incident.executed(r.remediation_id, True, "No action taken.", T0)
    events += incident.resolve(Resolution.ESCALATED, "Escalated to a person.", T0)
    assert "RemediationDeclined" in [e[0] for e in events]
    assert_valid(events)
