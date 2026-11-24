"""The chaos scenarios and an experiment's record (chaos scenarios doc section 2, ADR-0020).

Rules kept here, free of any I/O:

- Each scenario **affects** some services: the ones whose alerts it makes fire. Two experiments may
  run at once only if they affect different services (the owner's overlapping rehearsal: the leak
  grows while the bad deploy runs), so every incident the agent opens still has one cause.
- An experiment ends once: REMEDIATED (the agent's incident was resolved RECOVERED), STOPPED (by
  the presenter: stop or reset) or EXPIRED (its duration ran out). Whatever ends it, the fault is
  undone.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any


class ScenarioId(StrEnum):
    BAD_DEPLOY = "BAD_DEPLOY"
    MEMORY_LEAK = "MEMORY_LEAK"
    TRAFFIC_SPIKE = "TRAFFIC_SPIKE"
    PORTFOLIO_DB_DEGRADED = "PORTFOLIO_DB_DEGRADED"


class Status(StrEnum):
    RUNNING = "RUNNING"
    REMEDIATED = "REMEDIATED"
    STOPPED = "STOPPED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True)
class Scenario:
    scenario: ScenarioId
    name: str
    description: str
    target_service: str  # where the fault is put
    affects: frozenset[str]  # whose alerts it makes fire (the first is the primary)
    primary: str  # the service whose incident measures detection and recovery
    expected_fix: str
    default_duration_seconds: int
    defaults: dict[str, Any]


SCENARIOS: dict[ScenarioId, Scenario] = {
    s.scenario: s
    for s in (
        Scenario(
            ScenarioId.BAD_DEPLOY,
            "Bad deploy",
            "Deploys release 1.4.1 of order-service, which fails about 40% of order placements.",
            "order-service",
            frozenset({"order-service"}),
            "order-service",
            "rollback_deployment",
            300,
            {"error_rate": 0.4},
        ),
        Scenario(
            ScenarioId.MEMORY_LEAK,
            "Memory leak",
            "Makes market-data-service leak memory steadily until it is restarted.",
            "market-data-service",
            frozenset({"market-data-service"}),
            "market-data-service",
            "restart_deployment",
            600,
            {"rate_mb_per_minute": 80},
        ),
        Scenario(
            ScenarioId.TRAFFIC_SPIKE,
            "Panic sell",
            "The bots panic and dump shares at market: 110 requests per second at order-service"
            " through the gateway, more than 2 replicas can serve.",
            "order-service",
            frozenset({"order-service"}),
            "order-service",
            "scale_deployment",
            300,
            {"requests_per_second": 110},
        ),
        Scenario(
            ScenarioId.PORTFOLIO_DB_DEGRADED,
            "Portfolio database degraded",
            "Makes portfolio-service's database slow, so reservations time out and order-service's"
            " circuit breaker opens. The cause is a data store the agent may never touch.",
            "portfolio-service",
            frozenset({"order-service", "portfolio-service"}),
            "order-service",
            "do_nothing",
            300,
            {"delay_ms": 1500},
        ),
    )
}


@dataclass
class TimelineEntry:
    at: datetime
    message: str


@dataclass
class Experiment:
    experiment_id: str
    scenario: Scenario
    parameters: dict[str, Any]
    started_at: datetime
    duration_seconds: int
    status: Status = Status.RUNNING
    ended_at: datetime | None = None
    incident_id: str | None = None
    time_to_detect_seconds: int | None = None
    time_to_recover_seconds: int | None = None
    agent_actions: list[str] = field(default_factory=list)
    timeline: list[TimelineEntry] = field(default_factory=list)
    replicas_before: int | None = None  # order-service's replicas before a traffic spike

    @property
    def ends_at(self) -> datetime:
        return self.started_at + timedelta(seconds=self.duration_seconds)

    @property
    def running(self) -> bool:
        return self.status is Status.RUNNING

    def log(self, at: datetime, message: str) -> None:
        self.timeline.append(TimelineEntry(at, message))

    def end(self, status: Status, at: datetime, message: str) -> None:
        if not self.running:
            raise ValueError(f"experiment {self.experiment_id} already ended ({self.status})")
        self.status, self.ended_at = status, at
        self.log(at, message)


def conflicting(scenario: Scenario, running: list[Experiment]) -> Experiment | None:
    """The running experiment that affects a service this scenario would affect too, if any."""
    for other in running:
        if other.running and other.scenario.affects & scenario.affects:
            return other
    return None
