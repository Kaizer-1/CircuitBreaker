"""api-gateway's own Prometheus metrics (listed in its README)."""

from dataclasses import dataclass

from prometheus_client import CollectorRegistry, Gauge


@dataclass(frozen=True)
class BusinessMetrics:
    ws_connections: Gauge


def register_business_metrics(registry: CollectorRegistry) -> BusinessMetrics:
    return BusinessMetrics(
        ws_connections=Gauge(
            "gateway_ws_connections",
            "WebSocket connections proxied right now, by channel path.",
            ["channel"],
            registry=registry,
        ),
    )
