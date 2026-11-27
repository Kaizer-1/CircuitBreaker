"""An incident's life: statuses, the pending proposal, actions taken and the timing fields."""

from datetime import UTC, datetime, timedelta

import pytest

from sre_agent.domain.actions import Proposal, Tool
from sre_agent.domain.incident import Incident, NotPendingError, Resolution, Status

T0 = datetime(2026, 11, 26, 11, 5, 31, tzinfo=UTC)


def opened() -> Incident:
    incident, _ = Incident.open(
        7, "OrderServiceHighErrorRate", "order-service", "critical", "errors at 38%", T0,
        fired_at=T0 - timedelta(seconds=6),
    )  # fmt: skip
    return incident


def test_approval_then_execution_then_recovery() -> None:
    i = opened()
    assert i.incident_id == "INC-0007" and i.status is Status.OPEN
    p = Proposal(Tool.ROLLBACK.value, "order-service", None, "Bad release.", "llm:m")
    r, _ = i.propose(p, p.explanation, {"to_revision": 7}, True, T0 + timedelta(seconds=13))
    assert i.status.value == "AWAITING_APPROVAL" and i.pending is r
    i.approve(r.remediation_id, "presenter", None, T0 + timedelta(seconds=20))
    with pytest.raises(NotPendingError, match="already approved"):
        i.decline(r.remediation_id, "too late", T0, admin="presenter")
    i.executed(r.remediation_id, True, "Rolled back.", T0 + timedelta(seconds=22))
    i.recovered_at = T0 + timedelta(seconds=50)
    i.resolve(Resolution.RECOVERED, "Resolved.", T0 + timedelta(seconds=110))
    s = i.to_summary()
    assert (s["status"], s["resolution"], s["actions_taken"]) == (
        "RESOLVED", "RECOVERED", ["rollback_deployment"])  # fmt: skip
    assert s["time_to_recover_seconds"] == 50  # to the alert clearing, not the 60 s check
    assert s["mitigated_at"] == "2026-11-26T11:05:53.000Z"
    assert [e["kind"] for e in i.to_api()["timeline"]] == [
        "OPENED", "PROPOSED", "APPROVED", "EXECUTED", "RESOLVED"]  # fmt: skip


@pytest.mark.parametrize(
    ("git", "line"),
    [
        (
            {
                "status": "PR_OPENED",
                "pull_request_url": "https://github.com/x/y/pull/25",
                "reason": "From Git (894b0c42af73): revert pull request opened.",
            },
            "Git: From Git (894b0c42af73): revert pull request opened."
            " https://github.com/x/y/pull/25 (a person merges it).",
        ),
        (
            {"status": "NOT_NEEDED", "pull_request_url": None, "reason": "It was OutOfSync."},
            "Git: no revert needed. It was OutOfSync.",
        ),
        (
            {"status": "FAILED", "pull_request_url": None, "reason": "Manual git revert needed."},
            "Git: Manual git revert needed.",
        ),
    ],
)
def test_a_rollback_says_on_the_feed_what_its_git_step_did(git: dict[str, str], line: str) -> None:
    """The feed shows only the timeline: the Git step's result must be on it (SRE design 7)."""
    i = opened()
    p = Proposal(Tool.ROLLBACK.value, "order-service", None, "Bad release.", "llm:m")
    r, _ = i.propose(p, p.explanation, {"to_revision": 7}, False, T0)
    i.executed(r.remediation_id, True, "Rolled back.", T0, git_reconciliation=git)
    assert [(e.kind.value, e.message) for e in i.timeline[-2:]] == [
        ("EXECUTED", "Rolled back."),
        ("EXECUTED", line),
    ]


def test_do_nothing_is_a_decision_not_an_action_and_escalation_has_no_recovery_time() -> None:
    i = opened()
    p = Proposal(Tool.NOTHING.value, None, None, "Cause elsewhere.", "fixed rule")
    r, _ = i.propose(p, p.explanation, {}, False, T0)
    i.executed(r.remediation_id, True, "No action taken.", T0)
    i.resolve(Resolution.ESCALATED, "A person takes over.", T0 + timedelta(seconds=3))
    assert i.actions_taken == [] and i.mitigated_at is None
    assert i.time_to_recover_seconds is None and i.timeline[-1].kind.value == "ESCALATED"


def test_resolving_declines_a_proposal_still_waiting_and_an_admin_decline_names_them() -> None:
    i = opened()
    p = Proposal(Tool.SCALE.value, "order-service", 5, "Load.", "llm:m")
    r, _ = i.propose(p, p.explanation, {"replicas": 5}, True, T0)
    events = i.resolve(Resolution.AUTO_CLEARED, "Cleared on its own.", T0)
    assert [e[0] for e in events] == ["RemediationDeclined", "IncidentResolved"]
    assert r.decided_by == "guardrail" and i.resolve(Resolution.ESCALATED, "x", T0) == []
    j = opened()
    r2, _ = j.propose(p, p.explanation, {"replicas": 5}, True, T0)
    ((_, data),) = j.decline(r2.remediation_id, "Not now.", T0, admin="presenter")
    assert (data["declined_by"], r2.decided_by) == ("admin", "presenter")


def test_a_guardrail_refusal_never_names_a_data_store_as_its_target() -> None:
    i = opened()
    bad = Proposal(Tool.RESTART.value, "cassandra", None, "Restart the DB.", "llm:m")
    r, events = i.decline_proposal(bad, bad.explanation, "Not allowed.", T0)
    assert r.target_service is None and r.status.value == "DECLINED"
    assert events[0][1]["declined_by"] == "guardrail"
