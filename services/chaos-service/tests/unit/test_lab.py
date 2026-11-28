"""The chaos lab: start, overlap, stop, expiry, reset, and the agent's incidents on the timeline."""

import asyncio
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

import pytest

from chaos_service.application.lab import AlreadyRunningError, ChaosLab, NotRunningError
from chaos_service.domain.scenarios import ScenarioId, Status
from chaos_service.ports.targets import TargetUnavailableError
from chaos_service.runtime.events import EventEnvelope, format_timestamp
from tests.fakes import FakeHooks, FakeOrderService

T0 = datetime(2026, 11, 25, 9, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now


def make_lab() -> tuple[ChaosLab, FakeOrderService, FakeHooks, Clock]:
    deployment, hooks, clock = FakeOrderService(), FakeHooks(), Clock()
    ids = count(1)
    lab = ChaosLab(deployment, hooks, clock=clock, new_id=lambda: f"exp-{next(ids)}")
    return lab, deployment, hooks, clock


def event(event_type: str, at: datetime, **data: Any) -> EventEnvelope:
    return EventEnvelope(
        event_id=f"evt-{event_type}-{data.get('incident_id')}",
        event_type=event_type,
        event_version=1,
        occurred_at=format_timestamp(at),
        producer="sre-agent",
        environment="staging",
        correlation_id=data.get("incident_id"),
        causation_id=None,
        data=data,
    )


async def test_each_scenario_injects_its_fault_with_its_defaults() -> None:
    lab, deployment, hooks, _ = make_lab()
    bad = await lab.start(ScenarioId.BAD_DEPLOY)
    leak = await lab.start(ScenarioId.MEMORY_LEAK, parameters={"rate_mb_per_minute": 120})
    assert deployment.bad_release and deployment.calls == ["deploy 0.4"]
    assert hooks.on == {"leak": 120}
    assert bad.timeline[0].message.endswith("Deployed order-service release 1.4.1 (revision 8).")
    assert (leak.duration_seconds, leak.ends_at) == (600, T0 + timedelta(seconds=600))
    await lab.stop(bad.experiment_id)
    spike = await lab.start(ScenarioId.TRAFFIC_SPIKE)
    assert hooks.on["panic"] == (110, 300)
    assert spike.replicas_before == 2
    await lab.stop(spike.experiment_id)
    await lab.start(ScenarioId.PORTFOLIO_DB_DEGRADED, duration_seconds=180)
    assert hooks.on["database"] == 1500


async def test_experiments_overlap_only_when_they_affect_different_services() -> None:
    lab, *_ = make_lab()
    await lab.start(ScenarioId.MEMORY_LEAK)
    bad = await lab.start(ScenarioId.BAD_DEPLOY)  # different service: allowed (the rehearsal)
    for second in (ScenarioId.TRAFFIC_SPIKE, ScenarioId.PORTFOLIO_DB_DEGRADED):
        with pytest.raises(AlreadyRunningError) as refused:
            await lab.start(second)  # both would affect order-service too
        assert refused.value.running is bad
    await lab.stop(bad.experiment_id)
    await lab.start(ScenarioId.PORTFOLIO_DB_DEGRADED)


async def test_a_target_that_does_not_answer_leaves_no_experiment() -> None:
    lab, deployment, _, _ = make_lab()
    deployment.down = True
    with pytest.raises(TargetUnavailableError):
        await lab.start(ScenarioId.BAD_DEPLOY)
    assert lab.experiments == []
    deployment.down = False
    await lab.start(ScenarioId.BAD_DEPLOY)  # nothing was left blocking order-service


async def test_stopping_undoes_the_fault_once() -> None:
    lab, deployment, hooks, _ = make_lab()
    leak = await lab.start(ScenarioId.MEMORY_LEAK)
    stopped = await lab.stop(leak.experiment_id)
    assert stopped is leak and leak.status is Status.STOPPED
    assert hooks.on == {} and leak.timeline[-1].message == "Stopped by the presenter. Leak cleared."
    with pytest.raises(NotRunningError):
        await lab.stop(leak.experiment_id)
    assert await lab.stop("no-such") is None


async def test_an_experiment_expires_by_itself_and_undoes_its_fault() -> None:
    deployment, hooks = FakeOrderService(), FakeHooks()
    lab = ChaosLab(deployment, hooks)  # real clock: 60 s is the shortest duration...
    experiment = await lab.start(ScenarioId.BAD_DEPLOY, duration_seconds=60)
    experiment.duration_seconds = 0  # ...so shorten it after the start
    lab._expiries[experiment.experiment_id].cancel()
    lab._expiries[experiment.experiment_id] = asyncio.create_task(lab._expire_later(experiment))
    await asyncio.sleep(0.05)
    assert experiment.status is Status.EXPIRED and not deployment.bad_release


async def test_reset_undoes_every_fault_from_what_is_running_even_after_a_restart() -> None:
    lab, deployment, hooks, _ = make_lab()
    await lab.start(ScenarioId.MEMORY_LEAK)
    spike = await lab.start(ScenarioId.TRAFFIC_SPIKE)
    await deployment.scale(5)  # the agent scaled up
    # chaos-service restarts: a new lab with no memory of what it started
    deployment.bad_release = True
    restarted = ChaosLab(deployment, hooks)
    reset = await restarted.reset()
    assert reset.undone == [
        "order-service's bad release removed",
        "market-data-service's leak cleared",
        "the panic sell stopped",
        "order-service back to 2 replicas (was 5)",
    ]
    assert reset.failed == [] and hooks.on == {} and not deployment.bad_release
    # the lab that remembers ends its experiments as STOPPED, with the reset on their timelines
    await lab.reset()
    assert spike.status is Status.STOPPED
    assert spike.timeline[-1].message == "Presenter reset: nothing was left to undo."


async def test_reset_puts_back_the_replicas_from_before_the_spike_and_reports_what_failed() -> None:
    lab, deployment, hooks, _ = make_lab()
    await deployment.scale(3)
    await lab.start(ScenarioId.TRAFFIC_SPIKE)
    await deployment.scale(5)
    hooks.down = {"database"}
    reset = await lab.reset()
    assert "order-service back to 3 replicas (was 5)" in reset.undone
    assert reset.failed == ["portfolio-service: database did not answer: ConnectError"]


async def test_the_agents_incident_times_the_experiment_and_a_recovery_remediates_it() -> None:
    lab, deployment, _, clock = make_lab()
    bad = await lab.start(ScenarioId.BAD_DEPLOY)
    opened = clock.advance(52)
    await lab.on_incident_event(
        event(
            "IncidentOpened",
            opened,
            incident_id="INC-0007",
            alert_name="OrderServiceHighErrorRate",
            service="order-service",
            severity="critical",
            summary="errors",
            opened_at=format_timestamp(opened),
        )
    )
    assert (bad.incident_id, bad.time_to_detect_seconds) == ("INC-0007", 52)
    executed = clock.advance(9)
    await lab.on_incident_event(
        event(
            "RemediationExecuted",
            executed,
            incident_id="INC-0007",
            action="rollback_deployment",
            outcome="SUCCEEDED",
        )
    )
    deployment.bad_release = False  # the agent's rollback removed it
    resolved = clock.advance(75)
    await lab.on_incident_event(
        event(
            "IncidentResolved",
            resolved,
            incident_id="INC-0007",
            resolution="RECOVERED",
            resolved_at=format_timestamp(resolved),
        )
    )
    assert bad.status is Status.REMEDIATED
    assert (bad.agent_actions, bad.time_to_recover_seconds) == (["rollback_deployment"], 136)
    assert bad.timeline[-1].message == "Remediated by the agent. The bad release was already gone."


async def test_an_escalated_incident_leaves_the_experiment_running_for_a_person() -> None:
    lab, _, hooks, clock = make_lab()
    degraded = await lab.start(ScenarioId.PORTFOLIO_DB_DEGRADED)
    opened = clock.advance(25)
    for service, incident in (("api-gateway", "INC-0011"), ("order-service", "INC-0012")):
        await lab.on_incident_event(
            event(
                "IncidentOpened",
                opened,
                incident_id=incident,
                alert_name="X",
                service=service,
                severity="critical",
                summary="s",
                opened_at=format_timestamp(opened),
            )
        )
    assert degraded.incident_id == "INC-0012"  # the primary service's incident, not the gateway's
    resolved = clock.advance(5)
    await lab.on_incident_event(
        event(
            "IncidentResolved",
            resolved,
            incident_id="INC-0012",
            resolution="ESCALATED",
            resolved_at=format_timestamp(resolved),
        )
    )
    assert degraded.running and hooks.on == {"database": 1500}


async def test_what_happened_reads_as_plain_english() -> None:
    """The control room shows these lines next to the agent's feed: never a function name, a
    status code or None (owner, 2026-11-27: "Agent proposed do_nothing on None")."""
    lab, _, _, clock = make_lab()
    panic = await lab.start(ScenarioId.TRAFFIC_SPIKE)
    opened = clock.advance(47)
    common = {"incident_id": "INC-0056"}
    for event_type, data in (
        (
            "IncidentOpened",
            {
                "alert_name": "OrderServiceHighErrorRate",
                "service": "order-service",
                "severity": "critical",
                "summary": "order-service errors at 14.65% (normally under 1%)",
                "opened_at": format_timestamp(opened),
            },
        ),
        (
            "RemediationProposed",
            {
                "action": "scale_deployment",
                "target_service": "order-service",
                "parameters": {"replicas": 5},
            },
        ),  # fmt: skip
        (
            "RemediationExecuted",
            {
                "action": "scale_deployment",
                "target_service": "order-service",
                "parameters": {"replicas": 5},
                "outcome": "SUCCEEDED",
                "details": "Scaled order-service from 2 to 5 replicas.",
            },
        ),  # fmt: skip
        (
            "RemediationProposed",
            {"action": "do_nothing", "target_service": None, "parameters": {}},
        ),
        (
            "IncidentResolved",
            {"resolution": "ESCALATED", "resolved_at": format_timestamp(opened)},
        ),
    ):
        await lab.on_incident_event(event(event_type, opened, **common, **data))
    lines = [t.message for t in panic.timeline[1:]]
    assert lines == [
        "The agent opened INC-0056: order-service errors at 14.65% (normally under 1%)."
        " Detected 47 s after the start.",
        "The agent proposed to scale order-service to 5 replicas.",
        "The agent acted: scaled order-service from 2 to 5 replicas.",
        "The agent proposed to do nothing.",
        "INC-0056 was handed to a person.",
    ]
    for line in lines:
        for raw in ("_deployment", "do_nothing", "None", "SUCCEEDED", "ESCALATED"):
            assert raw not in line, line
