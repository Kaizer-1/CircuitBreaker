"""Business metrics named in the docs. None are named for this service yet; add them here."""

from dataclasses import dataclass

from prometheus_client import CollectorRegistry


@dataclass(frozen=True)
class BusinessMetrics:
    pass


def register_business_metrics(registry: CollectorRegistry) -> BusinessMetrics:
    return BusinessMetrics()
