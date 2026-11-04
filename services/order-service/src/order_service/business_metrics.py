"""order-service's own Prometheus metrics (listed in its README)."""

from dataclasses import dataclass

from prometheus_client import CollectorRegistry, Counter, Gauge

from order_service.application.circuit_breaker import BreakerState


@dataclass(frozen=True)
class BusinessMetrics:
    orders_placed_total: Counter
    orders_rejected_total: Counter
    circuit_breaker_state: Gauge
    circuit_breaker_trips_total: Counter

    def breaker_state(self, dependency: str, state: BreakerState) -> None:
        """1 for the current state, 0 for the others (the usual Prometheus "state set")."""
        for s in BreakerState:
            self.circuit_breaker_state.labels(dependency, s.value).set(1 if s is state else 0)


def register_business_metrics(registry: CollectorRegistry) -> BusinessMetrics:
    return BusinessMetrics(
        orders_placed_total=Counter(
            "orders_placed_total",
            "Orders accepted by order-service, by order type and side.",
            ["order_type", "side"],
            registry=registry,
        ),
        orders_rejected_total=Counter(
            "orders_rejected_total",
            "Orders stored as REJECTED, by reason.",
            ["reason"],
            registry=registry,
        ),
        circuit_breaker_state=Gauge(
            "circuit_breaker_state",
            "Circuit breaker state per dependency: 1 for the current one (closed/open/half_open).",
            ["dependency", "state"],
            registry=registry,
        ),
        circuit_breaker_trips_total=Counter(
            "circuit_breaker_trips_total",
            "Times a circuit breaker opened, per dependency.",
            ["dependency"],
            registry=registry,
        ),
    )
