"""The guardrails decide; the model only proposes (SRE agent design section 5)."""

from datetime import UTC, datetime, timedelta

import pytest

from sre_agent.domain.actions import Proposal, Tool
from sre_agent.domain.guardrails import Situation, check

NOW = datetime(2026, 11, 25, 11, 0, tzinfo=UTC)
CALM = Situation(
    now=NOW,
    replicas={"order-service": 2, "market-data-service": 1, "portfolio-service": 2},
    has_previous_revision={"order-service": True, "market-data-service": True},
    revision_new={"order-service": True, "market-data-service": True},
)


def propose(
    tool: str,
    service: str | None = "order-service",
    replicas: int | None = None,
    explanation: str = "Because.",
) -> Proposal:
    return Proposal(tool, service, replicas, explanation, "llm:test")


@pytest.mark.parametrize(
    "proposal",
    [
        propose(Tool.ROLLBACK.value),
        propose(Tool.SCALE.value, replicas=5),
        propose(Tool.RESTART.value, "market-data-service"),
        propose(Tool.NOTHING.value, None),
    ],
)
def test_the_three_fixes_and_doing_nothing_pass_when_calm(proposal: Proposal) -> None:
    assert check(proposal, CALM).allowed


@pytest.mark.parametrize(
    ("proposal", "guardrail"),
    [
        (propose("delete_namespace"), "G1"),
        (propose("kubectl", "order-service"), "G1"),
        (propose(Tool.RESTART.value, "cassandra"), "G2"),  # a data store: never
        (propose(Tool.ROLLBACK.value, "postgres"), "G2"),
        (propose(Tool.SCALE.value, "redpanda", 3), "G2"),
        (propose(Tool.SCALE.value, "matching-engine", 2), "G2"),  # a single writer
        (propose(Tool.RESTART.value, "prometheus"), "G2"),
        (propose(Tool.SCALE.value, replicas=0), "G3"),  # "scale to 0"
        (propose(Tool.SCALE.value, replicas=6), "G3"),
        (propose(Tool.SCALE.value, replicas=None), "G3"),
        (propose(Tool.SCALE.value, "portfolio-service", 2), "G3"),  # no change
        (propose(Tool.ROLLBACK.value, "portfolio-service"), "G7"),  # no older revision
    ],
)
def test_anything_outside_the_allowlist_or_its_bounds_is_declined(
    proposal: Proposal, guardrail: str
) -> None:
    verdict = check(proposal, CALM)
    assert (verdict.allowed, verdict.guardrail) == (False, guardrail)
    assert verdict.reason


def test_one_step_changes_replicas_by_three_at_most() -> None:
    assert check(propose(Tool.SCALE.value, replicas=5), CALM).allowed  # 2 -> 5
    one = Situation(now=NOW, replicas={"order-service": 1})
    assert check(propose(Tool.SCALE.value, replicas=5), one).guardrail == "G3"  # 1 -> 5


def test_cooldown_one_at_a_time_and_budgets_escalate() -> None:
    recent = Situation(
        **{**CALM.__dict__, "last_action_at": {"order-service": NOW - timedelta(minutes=4)}}
    )
    verdict = check(propose(Tool.ROLLBACK.value), recent)
    assert (verdict.guardrail, verdict.escalate) == ("G5", True)
    assert "4 min ago" in verdict.reason
    later = Situation(
        **{**CALM.__dict__, "last_action_at": {"order-service": NOW - timedelta(minutes=5)}}
    )
    assert check(propose(Tool.ROLLBACK.value), later).allowed

    busy = Situation(**{**CALM.__dict__, "acting_on": frozenset({"order-service"})})
    assert check(propose(Tool.SCALE.value, replicas=4), busy).guardrail == "G5"

    for field, value in (("executed_this_incident", 3), ("executed_last_hour", 6)):
        spent = Situation(**{**CALM.__dict__, field: value})
        verdict = check(propose(Tool.RESTART.value, "market-data-service"), spent)
        assert (verdict.guardrail, verdict.escalate) == ("G6", True)
    assert check(
        propose(Tool.NOTHING.value, None), Situation(now=NOW, executed_last_hour=6)
    ).allowed


def test_paused_records_only_and_a_bad_explanation_is_replaced_not_trusted() -> None:
    paused = Situation(**{**CALM.__dict__, "paused": True})
    assert check(propose(Tool.ROLLBACK.value), paused).guardrail == "G8"
    long = check(propose(Tool.ROLLBACK.value, explanation="x" * 601), CALM)
    assert long.allowed and long.flagged
    assert long.explanation == "The agent proposed rollback_deployment(order-service)."
    empty = check(propose(Tool.NOTHING.value, None, explanation="   "), CALM)
    assert empty.flagged and empty.explanation == "The agent proposed do_nothing."


def test_a_known_good_release_is_never_rolled_back_whoever_proposes_it() -> None:
    """ADR-0021's pair, in code: after the agent's rollback the running release is known-good,
    so even a model that ignored the facts cannot roll back onto the bad release."""
    after_rollback = Situation(**{**CALM.__dict__, "revision_new": {"order-service": False}})
    verdict = check(propose(Tool.ROLLBACK.value), after_rollback)
    assert (verdict.allowed, verdict.guardrail, verdict.escalate) == (False, "G7", True)
    assert "known-good" in verdict.reason
    assert check(propose(Tool.SCALE.value, replicas=5), after_rollback).allowed


def test_the_proposal_must_be_about_the_incidents_own_service() -> None:
    """A log line can tell the model to act elsewhere (prompt injection): the incident's
    evidence is about one service, and only that service may be touched."""
    on_order = Situation(**{**CALM.__dict__, "incident_service": "order-service"})
    verdict = check(propose(Tool.RESTART.value, "market-data-service"), on_order)
    assert (verdict.allowed, verdict.guardrail) == (False, "G2")
    assert check(propose(Tool.RESTART.value, "order-service"), on_order).allowed
    assert check(propose(Tool.NOTHING.value, None), on_order).allowed
