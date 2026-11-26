"""The rule-based fallback (ADR-0021): error type, revision and load, never "a deploy" alone."""

import pytest

from sre_agent.domain.actions import Tool
from sre_agent.domain.declines import decline_reason
from sre_agent.domain.fallback import SOURCE, decide
from sre_agent.domain.signals import Signals

BAD_RELEASE = Signals(
    alert="OrderServiceHighErrorRate",
    service="order-service",
    errors_by_code={"INTERNAL_ERROR": 3.1},
    request_rate=8.0,
    normal_rate=7.5,
    revision_minutes=2,
    revision_known_good=False,
    replicas=2,
)
# The panic sell five minutes after the agent rolled the bad release back: that rollback was itself
# a new rollout (2 minutes old), but to the known-good template; the errors are OVERLOADED.
OVERLOAD_AFTER_ROLLBACK = Signals(
    alert="OrderServiceHighErrorRate",
    service="order-service",
    errors_by_code={"OVERLOADED": 31.0, "INTERNAL_ERROR": 0.2},
    request_rate=118.0,
    normal_rate=8.0,
    cpu_of_limit=0.97,
    revision_minutes=2,
    revision_known_good=True,
    replicas=2,
)


def test_errors_after_a_bad_release_are_rolled_back() -> None:
    proposal = decide(BAD_RELEASE)
    assert (proposal.tool, proposal.service, proposal.source) == (
        Tool.ROLLBACK.value,
        "order-service",
        SOURCE,
    )


@pytest.mark.parametrize("alert", ["OrderServiceHighErrorRate", "OrderServiceHighLatency"])
def test_overload_shortly_after_a_rollback_is_scaled_never_rolled_back(alert: str) -> None:
    proposal = decide(Signals(**{**OVERLOAD_AFTER_ROLLBACK.__dict__, "alert": alert}))
    assert (proposal.tool, proposal.service, proposal.replicas) == (
        Tool.SCALE.value,
        "order-service",
        5,
    )
    assert "load, not a bug" in proposal.explanation


def test_internal_errors_on_the_known_good_revision_are_escalated_not_rolled_back() -> None:
    known_good = Signals(**{**BAD_RELEASE.__dict__, "revision_known_good": True})
    old = Signals(**{**BAD_RELEASE.__dict__, "revision_minutes": 600})
    assert decide(known_good).tool == decide(old).tool == Tool.NOTHING.value


def test_the_leak_is_restarted_and_an_open_breaker_is_left_alone() -> None:
    leak = Signals(
        alert="MarketDataMemoryGrowth",
        service="market-data-service",
        memory_of_limit=0.74,
        memory_rising=True,
        revision_minutes=900,
        replicas=1,
    )
    assert (decide(leak).tool, decide(leak).service) == (Tool.RESTART.value, "market-data-service")
    gateway = Signals(
        alert="GatewayHighErrorRate",
        service="api-gateway",
        errors_by_code={"DEPENDENCY_UNAVAILABLE": 6.0},
        breaker_open=True,
    )
    assert decide(gateway).tool == Tool.NOTHING.value


@pytest.mark.parametrize("passed_on", [0.6, 1.0])
def test_gateway_errors_passed_on_from_order_service_leave_the_gateway_alone(
    passed_on: float,
) -> None:
    """A bad deploy or a panic sell makes the gateway's error alert fire too: its 5xx are
    order-service's answers, passed on. Rolling back or scaling the gateway would not help."""
    gateway = Signals(
        alert="GatewayHighErrorRate",
        service="api-gateway",
        request_rate=120.0,
        normal_rate=9.0,
        revision_minutes=1,  # even with a new gateway revision running
        passed_on_share=passed_on,
        upstream="order-service",
        replicas=2,
    )
    proposal = decide(gateway)
    assert proposal.tool == Tool.NOTHING.value
    assert "order-service" in proposal.explanation and "passed on" in proposal.explanation


def test_the_gateways_own_internal_errors_after_its_new_release_are_rolled_back() -> None:
    own = Signals(
        alert="GatewayHighErrorRate",
        service="api-gateway",
        errors_by_code={"INTERNAL_ERROR": 4.0},
        passed_on_share=0.1,
        revision_minutes=3,
        replicas=2,
    )
    assert (decide(own).tool, decide(own).service) == (Tool.ROLLBACK.value, "api-gateway")


def test_after_the_spike_it_scales_back_and_unknown_alerts_are_escalated() -> None:
    over = Signals(alert="OrderServiceOverProvisioned", service="order-service", replicas=5)
    assert (decide(over).tool, decide(over).replicas) == (Tool.SCALE.value, 2)
    unknown = decide(Signals(alert="DiskFull", service="cassandra"))
    assert unknown.tool == Tool.NOTHING.value and "Escalating" in unknown.explanation


def test_the_database_degraded_decline_names_the_data_store() -> None:
    reason = decline_reason(
        Signals(alert="OrderServiceCircuitOpen", service="order-service", database_slow=True)
    )
    assert "database" in reason and "data store" in reason and "may never touch" in reason
    assert "would not help" in reason and "Escalating" in reason
