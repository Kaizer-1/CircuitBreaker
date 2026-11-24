"""chaos-service's own metrics (README, "Metrics"). The Incident view annotates every injection
from them (Phase 7). The SRE agent's evidence never includes them: its diagnosis is blind."""

from dataclasses import dataclass

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from chaos_service.domain.scenarios import Experiment, ScenarioId

SECONDS = (10, 20, 30, 45, 60, 90, 120, 180, 240, 300, 450, 600, 900)


@dataclass(frozen=True)
class BusinessMetrics:
    chaos_experiments_started_total: Counter
    chaos_experiments_ended_total: Counter
    chaos_experiment_running: Gauge
    chaos_time_to_detect_seconds: Histogram
    chaos_time_to_recover_seconds: Histogram
    chaos_resets_total: Counter

    def started(self, scenario: str) -> None:
        self.chaos_experiments_started_total.labels(scenario).inc()
        self.chaos_experiment_running.labels(scenario).set(1)

    def ended(self, experiment: Experiment) -> None:
        scenario = experiment.scenario.scenario.value
        self.chaos_experiments_ended_total.labels(scenario, experiment.status.value).inc()
        self.chaos_experiment_running.labels(scenario).set(0)
        if experiment.time_to_detect_seconds is not None:
            self.chaos_time_to_detect_seconds.labels(scenario).observe(
                experiment.time_to_detect_seconds
            )
        if experiment.time_to_recover_seconds is not None:
            self.chaos_time_to_recover_seconds.labels(scenario).observe(
                experiment.time_to_recover_seconds
            )


def register_business_metrics(registry: CollectorRegistry) -> BusinessMetrics:
    metrics = BusinessMetrics(
        chaos_experiments_started_total=Counter(
            "chaos_experiments_started_total",
            "Chaos experiments started (A-10).",
            ["scenario"],
            registry=registry,
        ),
        chaos_experiments_ended_total=Counter(
            "chaos_experiments_ended_total",
            "Chaos experiments ended, by outcome: REMEDIATED, STOPPED or EXPIRED.",
            ["scenario", "outcome"],
            registry=registry,
        ),
        chaos_experiment_running=Gauge(
            "chaos_experiment_running",
            "1 while an experiment of this scenario runs.",
            ["scenario"],
            registry=registry,
        ),
        chaos_time_to_detect_seconds=Histogram(
            "chaos_time_to_detect_seconds",
            "Experiment start to the agent's IncidentOpened on the primary service (MTTD).",
            ["scenario"],
            buckets=SECONDS,
            registry=registry,
        ),
        chaos_time_to_recover_seconds=Histogram(
            "chaos_time_to_recover_seconds",
            "Experiment start to the agent's IncidentResolved RECOVERED.",
            ["scenario"],
            buckets=SECONDS,
            registry=registry,
        ),
        chaos_resets_total=Counter(
            "chaos_resets_total", "Presenter resets (A-20).", registry=registry
        ),
    )
    for scenario in ScenarioId:  # every series exists from the start (0), for dashboards
        metrics.chaos_experiment_running.labels(scenario.value).set(0)
        metrics.chaos_experiments_started_total.labels(scenario.value)
    return metrics
