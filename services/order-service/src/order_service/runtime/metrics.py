"""Prometheus metrics, named as in docs/05-coding-standards.md section 7.

Alert rules and the SRE agent's evidence collector query these names, so do not rename them without
updating docs/03-architecture/06-chaos-scenarios.md.
"""

import asyncio

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
        # Which error each Problem Details answer was: the SRE agent tells a bad release
        # (INTERNAL_ERROR) from overload (OVERLOADED) and an open breaker (DEPENDENCY_UNAVAILABLE).
        self.http_problems_total = Counter(
            "http_problems_total",
            "Problem Details responses by error code.",
            ["service", "route", "status", "code"],
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
        # Filled by watch_event_loop, in the services that start it (those on the order path).
        self.event_loop_lag_seconds = Histogram(
            "event_loop_lag_seconds",
            "How late the event loop woke from a timer: the time ready work waits for the CPU.",
            ["service"],
            buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5),
            registry=self.registry,
        )


async def watch_event_loop(metrics: Metrics, interval: float = 0.25) -> None:
    """Sleep `interval` again and again and record how much later than that the loop woke.

    One event loop runs every request of a pod; when it is late, every request waits, whether for
    the pod's CPU limit (throttling) or for code that blocked the loop. The lag shows which part of
    a slow request was waiting rather than working.
    """
    loop = asyncio.get_running_loop()
    lag = metrics.event_loop_lag_seconds.labels(metrics.service)
    while True:
        started = loop.time()
        await asyncio.sleep(interval)
        lag.observe(max(0.0, loop.time() - started - interval))


def metrics_router(metrics: Metrics) -> APIRouter:
    router = APIRouter()

    @router.get("/metrics", include_in_schema=False)
    async def scrape() -> Response:
        return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)

    return router
