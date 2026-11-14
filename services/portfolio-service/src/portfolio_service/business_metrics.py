"""portfolio-service's own Prometheus metrics (listed in its README)."""

from dataclasses import dataclass

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


@dataclass(frozen=True)
class BusinessMetrics:
    reservations_total: Counter
    trades_settled_total: Counter
    orphan_expiry_requests_total: Counter
    portfolio_ws_connections: Gauge
    postgres_transaction_seconds: Histogram

    def transaction(self, waited: float, ran: float) -> None:
        self.postgres_transaction_seconds.labels("pool_wait").observe(waited)
        self.postgres_transaction_seconds.labels("transaction").observe(ran)


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
        portfolio_ws_connections=Gauge(
            "portfolio_ws_connections",
            "Open Q-10 WebSocket connections on this replica.",
            registry=registry,
        ),
        postgres_transaction_seconds=Histogram(
            "postgres_transaction_seconds",
            "Postgres transactions: waiting for a pooled connection (pool_wait), then the work.",
            ["phase"],
            buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
            registry=registry,
        ),
    )
