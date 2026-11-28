"""A tiny cluster for the agent's tests: Deployments with their ReplicaSets, metrics that depend on
what runs, alerts that fire from those metrics, a clock the tests move.

The real collector, decider, guardrails and incident run against it, so a test is a scenario:
"order-service runs a bad release; the error alert fires; the agent rolls back; 30 s later the
alert clears". Time is virtual: a 4-minute verification takes milliseconds.
"""

import asyncio
import copy
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sre_agent.domain.actions import Proposal
from sre_agent.domain.evidence import Evidence
from sre_agent.domain.incident import Incident
from sre_agent.domain.revisions import RESTARTED_AT, ReplicaSetInfo
from sre_agent.ports.llm import Choice, LLMUnavailableError
from sre_agent.ports.observability import Alert
from sre_agent.ports.platform import DeploymentState, PlatformError, PodInfo
from sre_agent.ports.store import ActionRecord, AgentSettings
from sre_agent.runtime.events import EventEnvelope

T0 = datetime(2026, 11, 26, 11, 0, tzinfo=UTC)


class Clock:
    def __init__(self, start: datetime = T0) -> None:
        self.t = start

    def now(self) -> datetime:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)
        await asyncio.sleep(0)


def template(image: str, restarted: str | None = None) -> dict[str, Any]:
    annotations = {RESTARTED_AT: restarted} if restarted else {}
    return {
        "metadata": {"labels": {"app": "svc"}, "annotations": annotations},
        "spec": {"containers": [{"name": "svc", "image": image}]},
    }


@dataclass
class FakeDeployment:
    name: str
    desired: int
    replicasets: list[ReplicaSetInfo]
    calls: list[str] = field(default_factory=list)

    @property
    def current(self) -> ReplicaSetInfo:
        return max(self.replicasets, key=lambda rs: rs.revision)

    def image(self) -> str:
        return str(self.current.template["spec"]["containers"][0]["image"])


@dataclass
class Rule:
    name: str
    service: str
    severity: str = "critical"
    remediable: bool = True
    summary: str = ""
    firing: Callable[["World"], bool] = lambda w: True  # noqa: E731
    value: Callable[["World"], float] | None = None  # the alert's value (e.g. the lag)


# A value, or a function of the world (it changes after the agent acts). `<key>_series` entries
# are functions of minutes ago instead: the shape of the 15-minute window.
Metric = float | dict[str, float] | None | Callable[[Any], Any]


class World:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.deployments: dict[str, FakeDeployment] = {}
        self.rules: list[Rule] = []
        self.metrics: dict[str, dict[str, Metric]] = {}  # service -> key -> value
        self.acted_at: dict[str, datetime] = {}  # service -> when the agent last acted
        self.fail_next: str | None = None
        self.pods: dict[str, list[PodInfo]] = {}  # service -> its pods (default: all ready)

    def deploy(
        self, name: str, *revisions: tuple[int, float, str, str], desired: int = 2
    ) -> FakeDeployment:
        """revisions: (number, minutes ago, image, change-cause)."""
        sets = [
            ReplicaSetInfo(
                n, self.clock.now() - timedelta(minutes=m), template(img), cause, f"{name}-r{n}"
            )
            for n, m, img, cause in revisions
        ]
        d = FakeDeployment(name, desired, sets)
        self.deployments[name] = d
        return d

    def since_action(self, service: str) -> float | None:
        at = self.acted_at.get(service)
        return None if at is None else (self.clock.now() - at).total_seconds()

    def metric(self, service: str, key: str) -> Any:
        value = self.metrics.get(service, {}).get(key)
        return value(self) if callable(value) else value


def key_of(query: str) -> str:
    if "kube_deployment_spec_replicas" in query:
        return "replicas_desired"
    if "kube_deployment_status_replicas_ready" in query:
        return "replicas_ready"
    if "kube_replicaset_status_ready_replicas" in query:
        return "replicaset_ready"
    if "quantile_over_time" in query:
        return "normal_rate"
    if "by (code)" in query:
        return "errors_by_type"
    if "by (service)" in query:
        return "upstream_errors"
    if "postgres_transaction_seconds" in query:
        return "database_slow_base" if "offset" in query else "database_slow"
    if "circuit_breaker_state" in query:
        return "breaker_open"
    if "consumer_lag" in query:
        return "consumer_lag"
    if "deriv(" in query:
        return "memory_rising"
    if "container_memory_working_set_bytes" in query:
        return "memory_of_limit"
    if "container_cpu_usage" in query:
        return "cpu_of_limit"
    if "histogram_quantile" in query:
        return "p95"
    if "http_problems_total" in query:
        return "own_problems_5xx"
    if ") / (" in query:
        return "error_ratio"
    if 'status=~"5.."' in query:
        return "errors"
    return "rate"


def service_of(query: str) -> str:
    match = re.search(r'(?:service|container|deployment)="([^"]+)"', query)
    return match.group(1) if match else "order-service"


class FakeMetrics:
    def __init__(self, world: World) -> None:
        self.world = world
        self.queries: list[str] = []
        self.down = False

    async def firing_alerts(self) -> list[Alert]:
        if self.down:
            raise ConnectionError("prometheus down")
        w = self.world
        return [
            Alert(
                r.name,
                r.service,
                r.severity,
                r.remediable,
                r.summary or r.name,
                w.clock.now(),
                value=r.value(w) if r.value else None,
            )
            for r in w.rules
            if r.firing(w)
        ]

    async def value(self, query: str) -> float | None:
        self.queries.append(query)
        v = self.world.metric(service_of(query), key_of(query))
        return sum(v.values()) if isinstance(v, dict) else v

    async def by_label(self, query: str, label: str) -> dict[str, float]:
        self.queries.append(query)
        key = key_of(query)
        # The upstreams' errors are asked across services (service=~"..."): the gateway holds them.
        v = self.world.metric("api-gateway" if key == "upstream_errors" else service_of(query), key)
        return dict(v) if isinstance(v, dict) else {}

    async def series(self, query: str, minutes: int, step_seconds: int) -> list[float | None]:
        """A `<key>_series` metric is a function of minutes ago (the shape over the window);
        without one, the series is flat at the current value."""
        shape = self.world.metrics.get(service_of(query), {}).get(f"{key_of(query)}_series")
        points = minutes * 60 // step_seconds + 1
        if callable(shape):
            ago = [minutes - i * step_seconds / 60 for i in range(points)]
            return [float(shape(m)) for m in ago]
        v = await self.value(query)
        return [v] * points

    async def series_by_label(
        self, query: str, label: str, minutes: int, step_seconds: int
    ) -> dict[str, list[float | None]]:
        """`replicaset_ready`: {ReplicaSet name: its ready pods, oldest first} (none: unknown)."""
        v = self.world.metric(service_of(query), key_of(query))
        return dict(v) if isinstance(v, dict) else {}


class FakeLogs:
    def __init__(self, lines: dict[str, list[str]] | None = None) -> None:
        self.lines = lines or {}

    async def error_lines(self, service: str, minutes: int, limit: int) -> list[str]:
        return self.lines.get(service, [])[:limit]

    async def top_errors(self, service: str, minutes: int, k: int) -> list[tuple[str, int]]:
        lines = self.lines.get(service, [])
        return [(lines[0], len(lines))] if lines else []


class FakePlatform:
    """Rollback reuses the target ReplicaSet with the next number, as Kubernetes does."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.calls: list[str] = []

    def _d(self, service: str) -> FakeDeployment:
        if self.world.fail_next:
            reason, self.world.fail_next = self.world.fail_next, None
            raise PlatformError(reason)
        return self.world.deployments[service]

    async def deployment(self, service: str) -> DeploymentState:
        d = self.world.deployments[service]
        pods = self.world.pods.get(service, [])
        ready = sum(p.ready for p in pods) if pods else d.desired
        return DeploymentState(d.name, d.desired, ready, d.desired, list(d.replicasets), pods)

    async def rollback(self, service: str, to_revision: int) -> str:
        d = self._d(service)
        current = d.current.revision
        target = next(rs for rs in d.replicasets if rs.revision == to_revision)
        d.replicasets = [rs for rs in d.replicasets if rs is not target]
        d.replicasets.append(replace(target, revision=current + 1))
        self.calls.append(f"rollback {service} {to_revision}")
        self.world.acted_at[service] = self.world.clock.now()
        return f"Rolled {service} back to revision {to_revision}."

    async def scale(self, service: str, replicas: int) -> str:
        d = self._d(service)
        d.desired = replicas
        self.calls.append(f"scale {service} {replicas}")
        self.world.acted_at[service] = self.world.clock.now()
        return f"Scaled {service} to {replicas}."

    async def restart(self, service: str) -> str:
        d = self._d(service)
        tpl = copy.deepcopy(d.current.template)
        tpl["metadata"]["annotations"][RESTARTED_AT] = self.world.clock.now().isoformat()
        d.replicasets.append(
            ReplicaSetInfo(
                d.current.revision + 1, self.world.clock.now(), tpl, d.current.change_cause
            )
        )
        self.calls.append(f"restart {service}")
        self.world.acted_at[service] = self.world.clock.now()
        return f"Restarted {service}."


class FakeLLM:
    """Answers from a script: a Proposal, or an exception (LLMUnavailableError)."""

    def __init__(self, *answers: Proposal | Exception, model: str = "anthropic/test") -> None:
        self.answers = list(answers)
        self.seen: list[Evidence] = []
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    async def choose_action(self, evidence: Evidence, deadline_seconds: float) -> Choice:
        self.seen.append(evidence)
        answer = self.answers.pop(0) if self.answers else LLMUnavailableError("script ended")
        if isinstance(answer, Exception):
            raise answer
        return Choice(answer, self._model, 1.0, 100)


class MemoryStore:
    def __init__(self) -> None:
        self.incidents: dict[str, Incident] = {}
        self.saved_settings: dict[str, AgentSettings] = {}
        self.actions: list[ActionRecord] = []
        self.number = 0

    async def next_incident_number(self) -> int:
        self.number += 1
        return self.number

    async def save(self, incident: Incident) -> None:
        self.incidents[incident.incident_id] = incident

    async def get(self, incident_id: str) -> Incident | None:
        return self.incidents.get(incident_id)

    async def recent(self, limit: int, before: datetime | None = None) -> list[Incident]:
        rows = sorted(self.incidents.values(), key=lambda i: i.opened_at, reverse=True)
        return [i for i in rows if before is None or i.opened_at < before][:limit]

    async def settings(self, environment: str) -> AgentSettings | None:
        return self.saved_settings.get(environment)

    async def save_settings(self, settings: AgentSettings) -> None:
        self.saved_settings[settings.environment] = settings

    async def record_action(self, record: ActionRecord) -> None:
        self.actions.append(record)

    async def actions_since(self, since: datetime) -> list[ActionRecord]:
        return [a for a in self.actions if a.executed_at >= since]


class ListSink:
    def __init__(self) -> None:
        self.events: list[EventEnvelope] = []

    async def publish(self, event: EventEnvelope) -> None:
        self.events.append(event)

    def types(self, incident_id: str | None = None) -> list[str]:
        return [
            e.event_type
            for e in self.events
            if incident_id is None or e.correlation_id == incident_id
        ]
