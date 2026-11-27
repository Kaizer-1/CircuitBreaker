"""Business metrics named in the docs (SRE agent design section 8)."""

from dataclasses import dataclass

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


@dataclass(frozen=True)
class BusinessMetrics:
    sre_agent_incidents_total: Counter
    sre_agent_actions_total: Counter
    sre_agent_declined_total: Counter
    sre_agent_time_to_recover_seconds: Histogram
    sre_agent_llm_latency_seconds: Histogram
    sre_agent_llm_errors_total: Counter
    sre_agent_llm_tokens_total: Counter
    sre_agent_llm_cost_dollars_total: Counter
    sre_agent_actions_left_this_hour: Gauge
    sre_agent_cooldown_seconds: Gauge


def register_business_metrics(registry: CollectorRegistry) -> BusinessMetrics:
    return BusinessMetrics(
        sre_agent_incidents_total=Counter(
            "sre_agent_incidents_total",
            "Incidents by alert and resolution.",
            ["alert", "resolution"],
            registry=registry,
        ),
        sre_agent_actions_total=Counter(
            "sre_agent_actions_total",
            "Remediation actions by action, service and outcome.",
            ["action", "service", "outcome"],
            registry=registry,
        ),
        sre_agent_declined_total=Counter(
            "sre_agent_declined_total",
            "Remediations declined, by who and why.",
            ["by", "reason"],
            registry=registry,
        ),
        sre_agent_time_to_recover_seconds=Histogram(
            "sre_agent_time_to_recover_seconds",
            "Seconds from incident opened to resolved.",
            registry=registry,
        ),
        sre_agent_llm_latency_seconds=Histogram(
            "sre_agent_llm_latency_seconds",
            "LLM call latency in seconds.",
            registry=registry,
        ),
        sre_agent_llm_errors_total=Counter(
            "sre_agent_llm_errors_total",
            "Failed LLM calls.",
            registry=registry,
        ),
        sre_agent_llm_tokens_total=Counter(
            "sre_agent_llm_tokens_total",
            "LLM tokens used, by model.",
            ["model"],
            registry=registry,
        ),
        sre_agent_llm_cost_dollars_total=Counter(
            "sre_agent_llm_cost_dollars",
            "What OpenRouter charged for the agent's model calls (US dollars), by model.",
            ["model"],
            registry=registry,
        ),
        # The guardrails' budget, for the projector's Incident view (owner, 2026-11-24): read-only,
        # there is no runtime override.
        sre_agent_actions_left_this_hour=Gauge(
            "sre_agent_actions_left_this_hour",
            "Actions the hourly budget still allows (G6).",
            registry=registry,
        ),
        sre_agent_cooldown_seconds=Gauge(
            "sre_agent_cooldown_seconds",
            "Seconds left of a service's cooldown after the agent's last action on it (G5);"
            " 0: none.",
            ["service"],
            registry=registry,
        ),
    )
