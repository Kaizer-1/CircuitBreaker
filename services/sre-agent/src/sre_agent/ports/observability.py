"""What the agent reads: Prometheus (alerts, metrics) and Loki (error logs). Read-only, and only
the agent's own code calls them: the model has no read tools (ADR-0009)."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


class SourceUnavailableError(Exception):
    """Prometheus or Loki did not answer; evidence is collected without it."""


@dataclass(frozen=True)
class Alert:
    name: str
    service: str
    severity: str
    remediable: bool
    summary: str
    fired_at: datetime | None  # when it started FIRING (pending start + the rule's `for`)
    labels: dict[str, str] = field(default_factory=dict)


class Metrics(Protocol):
    async def firing_alerts(self) -> list[Alert]:
        """Firing alerts in the agent's own namespace (each environment's agent sees its own)."""
        ...

    async def value(self, query: str) -> float | None:
        """One number (the sum if the query returns several series); None if empty."""
        ...

    async def by_label(self, query: str, label: str) -> dict[str, float]: ...

    async def series(self, query: str, minutes: int, step_seconds: int) -> list[float | None]:
        """One series, oldest first, a point every `step_seconds` (None where there was none)."""
        ...


class Logs(Protocol):
    async def error_lines(self, service: str, minutes: int, limit: int) -> list[str]:
        """The newest error-level lines of one service, newest first."""
        ...

    async def top_errors(self, service: str, minutes: int, k: int) -> list[tuple[str, int]]:
        """The k most frequent error messages and how often each appeared."""
        ...
