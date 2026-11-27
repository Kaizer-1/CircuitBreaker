"""The evidence collector (SRE agent design section 2): plain code that asks Prometheus, Loki and
Kubernetes the same questions every time and writes the answers in our own vocabulary.

It also works out the three facts of ADR-0021 (error type, revision, load) once, for both the
model's evidence and the rule-based fallback, so the two can never read the metrics differently.
A source that does not answer is listed in `missing` and the rest is still collected: an incident
is never stuck because Loki is slow.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, TypeVar

from sre_agent.domain import revisions
from sre_agent.domain.actions import MAX_REPLICAS, SCALABLE
from sre_agent.domain.evidence import MAX_LINES, Evidence, scrub
from sre_agent.domain.incident import Incident
from sre_agent.domain.revisions import RevisionFacts
from sre_agent.domain.signals import Signals
from sre_agent.ports.observability import Alert, Logs, Metrics
from sre_agent.ports.platform import DeploymentState, Platform
from sre_agent.ports.store import ActionRecord

logger = logging.getLogger("collector")
T = TypeVar("T")

OPS = 'route!~"/healthz|/readyz|/metrics"'
UPSTREAMS = ("order-service", "portfolio-service", "market-data-service")
WINDOW_MINUTES = 15
STEP_SECONDS = 30
DATABASE_SLOW_PER_SECOND = 0.01  # portfolio transactions over 0.5 s (normally 0), and 5x normal
# Memory's derivative is never exactly 0 (measured at rest: +0.5 to +1 kB/s); the leak scenario
# grows 1.3 MB/s. "Rising" means more than 50 kB/s (3 MB a minute).
RISING_BYTES_PER_SECOND = 50_000
HISTORY_MINUTES = 720  # Prometheus keeps 12 hours: how far back a release's runs are known


@dataclass(frozen=True)
class Collected:
    evidence: Evidence
    signals: Signals
    revision: RevisionFacts | None
    deployment: DeploymentState | None


class Queries:
    """The PromQL, in one place (the metric names are the services' own, see their READMEs)."""

    def __init__(self, namespace: str, service: str) -> None:
        self.ns, self.svc = namespace, service
        self.sel = f'namespace="{namespace}",service="{service}",{OPS}'
        self.box = f'namespace="{namespace}",container="{service}"'

    def rate(self, extra: str = "", window: str = "1m") -> str:
        return f"sum(rate(http_requests_total{{{self.sel}{extra}}}[{window}]))"

    @property
    def errors(self) -> str:
        return self.rate(',status=~"5.."')

    @property
    def error_ratio(self) -> str:
        return f"({self.errors}) / ({self.rate()})"

    @property
    def errors_by_type(self) -> str:
        return f'sum by (code) (rate(http_problems_total{{{self.sel},status=~"5.."}}[1m]))'

    @property
    def p95(self) -> str:
        return (
            "histogram_quantile(0.95, sum by (le) (rate("
            f"http_request_duration_seconds_bucket{{{self.sel}}}[1m])))"
        )

    @property
    def normal_rate(self) -> str:
        """The median request rate over the previous hour, before the incident's 15 minutes."""
        return f"quantile_over_time(0.5, ({self.rate()})[60m:1m] offset {WINDOW_MINUTES}m)"

    @property
    def memory_of_limit(self) -> str:
        return (
            f"max(container_memory_working_set_bytes{{{self.box}}})"
            f" / max(container_spec_memory_limit_bytes{{{self.box}}} > 0)"
        )

    @property
    def memory_rising(self) -> str:
        return f"max(deriv(container_memory_working_set_bytes{{{self.box}}}[2m]))"

    @property
    def cpu_of_limit(self) -> str:
        return (
            f"sum(rate(container_cpu_usage_seconds_total{{{self.box}}}[1m]))"
            f' / sum(kube_pod_container_resource_limits{{{self.box},resource="cpu"}})'
        )

    @property
    def consumer_lag(self) -> str:
        return f'max(consumer_lag_messages{{namespace="{self.ns}",service="{self.svc}"}})'

    @property
    def breaker_open(self) -> str:
        return (
            f'max(max_over_time(circuit_breaker_state{{namespace="{self.ns}",'
            'dependency="portfolio-service",state="open"}[30s]))'
        )

    def database_slow(self, window: str = "2m", offset: str = "") -> str:
        """portfolio-service's database transactions slower than 0.5 s, per second. Normally none:
        a percentile over ALL transactions hides them, because once order-service's breaker
        opens only a few reservations reach Postgres while the fast transactions go on (found on
        staging, 2026-11-26: p95 7.9 ms during the degraded-database scenario)."""
        off = f" offset {offset}" if offset else ""
        sel = f'namespace="{self.ns}",phase="transaction"'
        return (
            f"sum(rate(postgres_transaction_seconds_count{{{sel}}}[{window}]{off}))"
            f' - sum(rate(postgres_transaction_seconds_bucket{{{sel},le="0.5"}}[{window}]{off}))'
        )

    @property
    def replicaset_ready(self) -> str:
        """Each of the service's ReplicaSets' ready pods: when each pod template RAN."""
        return (
            f'max by (replicaset) (kube_replicaset_status_ready_replicas{{namespace="{self.ns}",'
            f'replicaset=~"{self.svc}-[a-z0-9]+"}})'
        )

    @property
    def own_problems_5xx(self) -> str:
        return f'sum(rate(http_problems_total{{{self.sel},status=~"5.."}}[1m]))'

    @property
    def upstream_errors(self) -> str:
        names = "|".join(UPSTREAMS)
        return (
            f'sum by (service) (rate(http_requests_total{{namespace="{self.ns}",'
            f'service=~"{names}",status=~"5..",{OPS}}}[1m]))'
        )


GATEWAY = "api-gateway"


class EvidenceCollector:
    def __init__(
        self,
        namespace: str,
        metrics: Metrics,
        logs: Logs | None,
        platform: Platform,
        now: Callable[[], datetime],
    ) -> None:
        self._ns = namespace
        self._metrics = metrics
        self._logs = logs
        self._platform = platform
        self._now = now

    async def passed_on(self, service: str) -> tuple[float | None, str | None]:
        """For the gateway: the share of its 5xx that are its upstreams' own answers, passed on,
        and the upstream with the most errors. (None, None) for any other service, or when
        Prometheus has no 5xx to count. Raises if Prometheus does not answer."""
        if service != GATEWAY:
            return None, None
        q, m = Queries(self._ns, service), self._metrics
        own = await m.value(q.own_problems_5xx)
        all_5xx = await m.value(q.errors)
        share = max(0.0, min(1.0, (all_5xx - (own or 0)) / all_5xx)) if all_5xx else None
        ups = await m.by_label(q.upstream_errors, "service")
        upstream = max(ups, key=ups.__getitem__) if ups and max(ups.values()) > 0 else None
        return share, upstream

    async def collect(
        self,
        alert: Alert,
        others: list[Alert],
        incident: Incident,
        history: list[ActionRecord],
    ) -> Collected:
        now = self._now()
        missing: list[str] = []
        q = Queries(self._ns, alert.service)

        async def get(what: str, call: Awaitable[T], default: T) -> T:
            try:
                return await call
            except Exception as exc:  # any source may fail; the incident goes on without it
                logger.warning("evidence source failed", extra={"source": what, "error": str(exc)})
                if what not in missing:
                    missing.append(what)
                return default

        m = self._metrics
        numbers = [
            q.rate(),
            q.normal_rate,
            q.error_ratio,
            q.p95,
            q.memory_of_limit,
            q.memory_rising,
            q.cpu_of_limit,
            q.consumer_lag,
        ]
        values: list[float | None] = list(
            await asyncio.gather(*(get("prometheus", m.value(expr), None) for expr in numbers))
        )
        rate, normal, ratio, p95, memory, rising, cpu, lag = values
        no_types: dict[str, float] = {}
        by_type = await get("prometheus", m.by_label(q.errors_by_type, "code"), no_types)
        breaker = database = passed_on = None
        upstream: str | None = None
        if alert.service == "order-service":
            breaker = await get("prometheus", m.value(q.breaker_open), None)
        slow_per_minute = None
        if alert.service in ("order-service", "portfolio-service"):
            slow = await get("prometheus", m.value(q.database_slow()), None)
            base = await get(
                "prometheus", m.value(q.database_slow("1h", f"{WINDOW_MINUTES}m")), None
            )
            if slow is not None:
                slow_per_minute = 60 * slow
                database = slow > max(DATABASE_SLOW_PER_SECOND, 5 * (base or 0))
        if alert.service == GATEWAY:
            passed_on, upstream = await get(
                "prometheus", self.passed_on(alert.service), (None, None)
            )

        series_specs = {
            "requests_per_second": q.rate(),
            "error_ratio": q.error_ratio,
            "p95_latency_seconds": q.p95,
            "memory_of_limit": q.memory_of_limit,
            "cpu_of_limit": q.cpu_of_limit,
        }
        no_points: list[float | None] = []
        series_values: list[list[float | None]] = await asyncio.gather(
            *(
                get("prometheus", m.series(expr, WINDOW_MINUTES, STEP_SECONDS), no_points)
                for expr in series_specs.values()
            )
        )
        deployment = await get("kubernetes", self._platform.deployment(alert.service), None)
        lines: list[str] = []
        top: list[tuple[str, int]] = []
        if self._logs is not None:
            logs = self._logs
            lines = await get(
                "loki", logs.error_lines(alert.service, WINDOW_MINUTES, MAX_LINES), []
            )
            top = await get("loki", logs.top_errors(alert.service, WINDOW_MINUTES, 5), [])

        no_runs: dict[str, list[float | None]] = {}
        ready = await get(
            "prometheus",
            m.series_by_label(q.replicaset_ready, "replicaset", HISTORY_MINUTES, 60),
            no_runs,
        )
        activity = {name: revisions.spans_from_series(pts, now, 60) for name, pts in ready.items()}
        rev = revisions.facts(deployment.replicasets, now, activity) if deployment else None
        errors = {code: v for code, v in by_type.items() if v > 0}
        signals = Signals(
            alert=alert.name,
            service=alert.service,
            errors_by_code=errors,
            request_rate=rate or 0.0,
            normal_rate=normal or 0.0,
            cpu_of_limit=cpu or 0.0,
            memory_of_limit=memory or 0.0,
            memory_rising=(rising or 0) > RISING_BYTES_PER_SECOND,
            revision_minutes=rev.running_minutes if rev else None,
            revision_known_good=rev.known_good if rev else False,
            replicas=deployment.desired if deployment else 0,
            breaker_open=bool(breaker),
            database_slow=bool(database),
            passed_on_share=passed_on or 0.0,
            upstream=upstream,
        )
        facts: dict[str, Any] = {
            "error_ratio": _round(ratio),
            "errors_per_second_by_type": {k: round(v, 2) for k, v in errors.items()},
            "main_error_type": signals.main_error,
            "request_rate": _round(rate),
            "normal_rate": _round(normal),
            "load_ratio": round(rate / normal, 1) if rate and normal else None,
            "p95_latency_seconds": _round(p95),
            "cpu_of_limit": _round(cpu),
            "memory_of_limit": _round(memory),
            "memory_rising": signals.memory_rising,
            "consumer_lag_messages": lag,
        }
        if rev is not None:
            facts["revision"] = {
                "number": rev.revision,
                "release": rev.release,
                "new": rev.new,
                "running_for_minutes": round(rev.running_minutes, 1),
                "longest_run_minutes_last_12h": round(rev.longest_run_minutes, 1),
                "rollback_target": (
                    {"revision": rev.rollback_to, "release": rev.rollback_release}
                    if rev.rollback_to
                    else None
                ),
            }
        if deployment is not None:
            facts["replicas"] = {"desired": deployment.desired, "ready": deployment.ready}
            if rev is not None and deployment.pods:
                current = [p for p in deployment.pods if p.revision == rev.revision]
                older = [p for p in deployment.pods if p.revision != rev.revision]
                facts["rollout"] = {
                    "current_revision_ready_pods": sum(p.ready for p in current),
                    "current_revision_pods": len(current),
                    "older_revisions_ready_pods": sum(p.ready for p in older),
                    "current_revision_restarts": sum(p.restarts for p in current),
                }
        if breaker is not None:
            facts["breaker_to_portfolio_open"] = bool(breaker)
        if database is not None:
            facts["portfolio_database_slow"] = database
            facts["portfolio_transactions_over_half_a_second_per_minute"] = round(
                slow_per_minute or 0, 1
            )
        if passed_on is not None:
            facts["passed_on_share"] = round(passed_on, 2)
            facts["upstream_with_most_errors"] = upstream

        hour_ago = now - timedelta(hours=1)
        evidence = Evidence(
            alert={
                "name": alert.name,
                "service": alert.service,
                "severity": alert.severity,
                "summary": alert.summary,
                "firing_for_seconds": (
                    round((now - alert.fired_at).total_seconds()) if alert.fired_at else None
                ),
            },
            other_alerts=sorted({a.name for a in others if a.name != alert.name}),
            facts=facts,
            metrics={
                name: {"step_seconds": STEP_SECONDS, "points": [_round(v) for v in values]}
                for name, values in zip(series_specs, series_values, strict=True)
                if any(v is not None for v in values)
            },
            logs={
                "top_errors": [{"message": scrub(msg), "count": n} for msg, n in top],
                "recent": [scrub(line) for line in lines[:MAX_LINES]],
            },
            deploy_history=revisions.history(deployment.replicasets, now) if deployment else [],
            pods=[
                {
                    "name": p.name,
                    "revision": p.revision,
                    "of_current_revision": bool(rev and p.revision == rev.revision),
                    "phase": p.phase,
                    "ready": p.ready,
                    "restarts": p.restarts,
                    "last_termination": p.last_termination,
                }
                for p in (deployment.pods if deployment else [])
            ],
            agent_history={
                "this_incident": [
                    {"action": r.action, "status": r.status.value, "explanation": r.explanation}
                    for r in incident.remediations
                ],
                "this_service_last_hour": [
                    {
                        "action": h.action,
                        "minutes_ago": round((now - h.executed_at).total_seconds() / 60, 1),
                    }
                    for h in history
                    if h.service == alert.service and h.executed_at >= hour_ago
                ],
            },
            limits={
                "scalable": alert.service in SCALABLE,
                "max_replicas": MAX_REPLICAS if alert.service in SCALABLE else None,
                "replicas_now": deployment.desired if deployment else None,
            },
            missing=missing,
        )
        return Collected(evidence, signals, rev, deployment)


def _round(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)
