"""Business metrics of market-data-service (named in its README)."""

from dataclasses import dataclass

from prometheus_client import CollectorRegistry, Counter, Gauge


@dataclass(frozen=True)
class BusinessMetrics:
    market_ws_connections: Gauge
    candles_closed_total: Counter
    projection_gaps_filled_total: Counter
    time_travel_active: Gauge


def register_business_metrics(registry: CollectorRegistry) -> BusinessMetrics:
    return BusinessMetrics(
        market_ws_connections=Gauge(
            "market_ws_connections",
            "Open Q-06 WebSocket connections on this replica.",
            ["service"],
            registry=registry,
        ),
        candles_closed_total=Counter(
            "candles_closed_total",
            "Candles closed (and CandleClosed published), by interval.",
            ["service", "interval"],
            registry=registry,
        ),
        projection_gaps_filled_total=Counter(
            "projection_gaps_filled_total",
            "Times a gap in matching.v1 was filled from the event store.",
            ["service"],
            registry=registry,
        ),
        time_travel_active=Gauge(
            "time_travel_active",
            "Time-travel work running on this replica: replay (Q-13 streams) or rebuild (Q-12).",
            ["service", "kind"],
            registry=registry,
        ),
    )
