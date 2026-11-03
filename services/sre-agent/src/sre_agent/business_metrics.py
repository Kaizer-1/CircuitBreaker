"""Business metrics named in the docs. Registered now at zero; later phases update them."""

from dataclasses import dataclass

from prometheus_client import CollectorRegistry, Counter, Histogram


@dataclass(frozen=True)
class BusinessMetrics:
    sre_agent_incidents_total: Counter
    sre_agent_actions_total: Counter
    sre_agent_declined_total: Counter
    sre_agent_time_to_recover_seconds: Histogram
    sre_agent_llm_latency_seconds: Histogram
    sre_agent_llm_errors_total: Counter
    sre_agent_llm_tokens_total: Counter


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
    )
