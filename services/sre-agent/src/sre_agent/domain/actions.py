"""The agent's four tools and which services each may touch (SRE agent design section 3, ADR-0009).

This is the whole allowlist. It is code, changed only by a reviewed pull request: there is no API,
setting or model answer that adds a tool or a target. Data stores (Cassandra, Postgres, Redpanda)
and the observability stack are in no list, so no tool can name them.
"""

from dataclasses import dataclass
from enum import StrEnum


class Tool(StrEnum):
    ROLLBACK = "rollback_deployment"
    SCALE = "scale_deployment"
    RESTART = "restart_deployment"
    NOTHING = "do_nothing"


APPLICATION_SERVICES = frozenset(
    {
        "api-gateway",
        "order-service",
        "matching-engine",
        "market-data-service",
        "portfolio-service",
        "bot-traders",
        "sre-agent",
        "chaos-service",
    }
)
# Only these may get more replicas: the others are single writers (one replica on purpose) or do not
# serve traffic that more copies would share.
SCALABLE = frozenset({"api-gateway", "order-service", "market-data-service", "portfolio-service"})
TARGETS: dict[Tool, frozenset[str]] = {
    Tool.ROLLBACK: APPLICATION_SERVICES,
    Tool.SCALE: SCALABLE,
    Tool.RESTART: APPLICATION_SERVICES,
    Tool.NOTHING: frozenset(),
}
MAX_REPLICAS = 5
MAX_STEP = 3  # replicas changed by one action at most
NORMAL_REPLICAS = 2  # the chart's default for the scalable services


@dataclass(frozen=True)
class Proposal:
    """One tool call, from the model or from the rule-based fallback."""

    tool: str  # a Tool value, but checked: a model can answer anything
    service: str | None
    replicas: int | None
    explanation: str
    source: str  # the model's OpenRouter slug, "rule-based (LLM unavailable)" or a fixed rule

    @property
    def summary(self) -> str:
        if self.tool == Tool.SCALE:
            return f"{self.tool}({self.service}, {self.replicas})"
        if self.tool == Tool.NOTHING:
            return Tool.NOTHING.value
        return f"{self.tool}({self.service})"
