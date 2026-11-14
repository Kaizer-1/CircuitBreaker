"""order-service's own Prometheus metrics (listed in its README)."""

from dataclasses import dataclass

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from order_service.application.circuit_breaker import BreakerState

# Finer at the bottom than the HTTP buckets: a stage is a part of one request.
STAGE_BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)


@dataclass(frozen=True)
class BusinessMetrics:
    orders_placed_total: Counter
    orders_rejected_total: Counter
    circuit_breaker_state: Gauge
    circuit_breaker_trips_total: Counter
    order_place_stage_seconds: Histogram
    cassandra_call_seconds: Histogram

    def cassandra_call(self, waited: float, ran: float) -> None:
        self.cassandra_call_seconds.labels("thread_wait").observe(waited)
        self.cassandra_call_seconds.labels("execute").observe(ran)

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
        order_place_stage_seconds=Histogram(
            "order_place_stage_seconds",
            "Time A-05 spends in each stage of placing an order (claim, reserve, store, publish).",
            ["stage"],
            buckets=STAGE_BUCKETS,
            registry=registry,
        ),
        cassandra_call_seconds=Histogram(
            "cassandra_call_seconds",
            "Cassandra driver calls: waiting for a worker thread (thread_wait), then the call.",
            ["phase"],
            buckets=STAGE_BUCKETS,
            registry=registry,
        ),
    )
