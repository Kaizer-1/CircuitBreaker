"""The matching engine's own Prometheus metrics (listed in its README)."""

from dataclasses import dataclass

from prometheus_client import CollectorRegistry, Counter, Gauge


@dataclass(frozen=True)
class BusinessMetrics:
    trades_executed_total: Counter
    commands_processed_total: Counter
    closed_orders_remembered: Gauge


def register_business_metrics(registry: CollectorRegistry) -> BusinessMetrics:
    return BusinessMetrics(
        trades_executed_total=Counter(
            "trades_executed_total",
            "Trades executed by the matching engine, by ticker.",
            ["ticker"],
            registry=registry,
        ),
        commands_processed_total=Counter(
            "commands_processed_total",
            "orders.v1 commands handled, by command and outcome.",
            ["command", "outcome"],
            registry=registry,
        ),
        closed_orders_remembered=Gauge(
            "closed_orders_remembered",
            "Finished orders the book still remembers (about one hour of them), by ticker.",
            ["ticker"],
            registry=registry,
        ),
    )
