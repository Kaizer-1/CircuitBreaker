"""Prometheus metrics, named as in docs/05-coding-standards.md section 7.

Alert rules and the SRE agent's evidence collector query these names, so do not rename them without
updating docs/03-architecture/06-chaos-scenarios.md.
"""

from fastapi import APIRouter, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from prometheus_client.process_collector import ProcessCollector


class Metrics:
    """The standard metrics every service exposes, on a registry owned by this app instance."""

    def __init__(self, service: str) -> None:
        self.service = service
        self.registry = CollectorRegistry(auto_describe=True)
        ProcessCollector(registry=self.registry)  # process_resident_memory_bytes, CPU seconds, ...

        self.http_requests_total = Counter(
            "http_requests_total",
            "HTTP requests handled.",
            ["service", "route", "method", "status"],
            registry=self.registry,
        )
        self.http_request_duration_seconds = Histogram(
            "http_request_duration_seconds",
            "HTTP request latency in seconds.",
            ["service", "route", "method"],
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
            registry=self.registry,
        )
        self.http_requests_in_flight = Gauge(
            "http_requests_in_flight",
            "HTTP requests currently being handled.",
            ["service"],
            registry=self.registry,
        )
        # Event metrics: defined now, updated once services publish and consume events (Phase 2+).
        self.events_published_total = Counter(
            "events_published_total",
            "Events published.",
            ["service", "topic", "event_type"],
            registry=self.registry,
        )
        self.events_consumed_total = Counter(
            "events_consumed_total",
            "Events consumed.",
            ["service", "topic", "event_type"],
            registry=self.registry,
        )
        self.event_processing_errors_total = Counter(
            "event_processing_errors_total",
            "Events that failed processing.",
            ["service", "topic"],
            registry=self.registry,
        )
        self.events_dead_lettered_total = Counter(
            "events_dead_lettered_total",
            "Messages sent to <topic>.dlq after failing (DeadLetterMessages alert).",
            ["service", "topic"],
            registry=self.registry,
        )
        self.consumer_lag_messages = Gauge(
            "consumer_lag_messages",
            "Messages a consumer is behind the end of a partition.",
            ["service", "topic", "partition"],
            registry=self.registry,
        )


def metrics_router(metrics: Metrics) -> APIRouter:
    router = APIRouter()

    @router.get("/metrics", include_in_schema=False)
    async def scrape() -> Response:
        return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)

    return router
