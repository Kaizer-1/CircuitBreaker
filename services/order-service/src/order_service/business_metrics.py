"""Business metrics named in the docs. Registered now at zero; later phases update them."""

from dataclasses import dataclass

from prometheus_client import CollectorRegistry, Counter


@dataclass(frozen=True)
class BusinessMetrics:
    orders_placed_total: Counter


def register_business_metrics(registry: CollectorRegistry) -> BusinessMetrics:
    return BusinessMetrics(
        orders_placed_total=Counter(
            "orders_placed_total",
            "Orders accepted by order-service, by order type and side.",
            ["order_type", "side"],
            registry=registry,
        ),
    )
