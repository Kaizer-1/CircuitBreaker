"""api-gateway's own Prometheus metrics (listed in its README)."""

from dataclasses import dataclass

from prometheus_client import CollectorRegistry, Counter, Gauge


@dataclass(frozen=True)
class BusinessMetrics:
    ws_connections: Gauge
    upstream_connections_opened: Counter


def register_business_metrics(registry: CollectorRegistry) -> BusinessMetrics:
    return BusinessMetrics(
        ws_connections=Gauge(
            "gateway_ws_connections",
            "WebSocket connections proxied right now, by channel path.",
            ["channel"],
            registry=registry,
        ),
        # A Kubernetes Service balances connections, not requests: how often the gateway opens a
        # new one shows whether load can move to pods added by a scale-up (deployment doc).
        upstream_connections_opened=Counter(
            "gateway_upstream_connections_opened_total",
            "TCP connections the gateway opened to an upstream service (keep-alive reuses them).",
            ["upstream"],
            registry=registry,
        ),
    )
