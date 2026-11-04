"""portfolio-service's own Prometheus metrics (listed in its README)."""

from dataclasses import dataclass

from prometheus_client import CollectorRegistry, Counter


@dataclass(frozen=True)
class BusinessMetrics:
    reservations_total: Counter
    trades_settled_total: Counter
    orphan_expiry_requests_total: Counter


def register_business_metrics(registry: CollectorRegistry) -> BusinessMetrics:
    return BusinessMetrics(
        reservations_total=Counter(
            "reservations_total",
            "Reservation requests (A-06), by side and result (created, repeat, refused).",
            ["side", "result"],
            registry=registry,
        ),
        trades_settled_total=Counter(
            "trades_settled_total", "TradeExecuted events settled.", registry=registry
        ),
        orphan_expiry_requests_total=Counter(
            "orphan_expiry_requests_total",
            "Expiry requests accepted by order-service for orphaned reservations (ADR-0017).",
            registry=registry,
        ),
    )
