"""order-service app factory.

Run with: uvicorn order_service.main:create_app --factory.

The portfolio client and its circuit breaker exist from the start (so the breaker's metrics do too).
A background task connects to Cassandra and Redpanda, applies the schema, then runs the outbox
relay and the matching.v1 consumer. The HTTP API answers 503 until the store is ready.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from order_service.adapters.cassandra_store import CassandraOrderStore, connect_cassandra
from order_service.adapters.portfolio_http import HttpPortfolio
from order_service.api import orders as orders_api
from order_service.application.circuit_breaker import CircuitBreaker
from order_service.application.orders import Orders
from order_service.application.outbox_relay import OutboxRelay
from order_service.business_metrics import register_business_metrics
from order_service.config import Settings
from order_service.runtime.checks import cassandra_check, redpanda_check
from order_service.runtime.events import EventEnvelope
from order_service.runtime.health import ReadinessCheck, health_router
from order_service.runtime.jsonlog import configure_logging
from order_service.runtime.kafka import TRANSIENT_ERRORS, Delivery, EventConsumer, EventPublisher
from order_service.runtime.metrics import Metrics, metrics_router
from order_service.runtime.middleware import ObservabilityMiddleware
from order_service.runtime.problem import install_exception_handlers

# Topics this service produces or consumes, per the event catalog.
TOPICS = ("orders.v1", "matching.v1")
CONSUMER_GROUP = "order-service.matching.v1"

logger = logging.getLogger("startup")


def default_checks(settings: Settings) -> list[ReadinessCheck]:
    return [
        redpanda_check(settings.kafka_bootstrap_servers, TOPICS),
        cassandra_check(
            settings.cassandra_contact_points.split(","),
            settings.cassandra_port,
            settings.cassandra_keyspace,
            settings.cassandra_username,
            settings.cassandra_password.get_secret_value(),
        ),
    ]


def transient_errors() -> tuple[type[BaseException], ...]:
    """Infrastructure errors: retried until they recover, never dead-lettered (runtime/kafka.py)."""
    from aiokafka.errors import KafkaError
    from cassandra import OperationTimedOut, Timeout, Unavailable
    from cassandra.cluster import NoHostAvailable

    return (*TRANSIENT_ERRORS, KafkaError, OperationTimedOut, Timeout, Unavailable, NoHostAvailable)


async def run_service(app: FastAPI, settings: Settings, metrics: Metrics) -> None:
    publisher = EventPublisher(settings.kafka_bootstrap_servers, settings.service_name, metrics)
    business = app.state.business_metrics
    while True:
        try:
            session = await asyncio.to_thread(
                connect_cassandra,
                settings.cassandra_contact_points.split(","),
                settings.cassandra_port,
                settings.cassandra_keyspace,
                settings.cassandra_username,
                settings.cassandra_password.get_secret_value(),
            )
            store = CassandraOrderStore(session)
            await store.migrate()
            await publisher.start()
            break
        except Exception:
            logger.exception("start-up failed, retrying in 3 s")
            await publisher.stop()
            await asyncio.sleep(3)

    def rejected(reason: object) -> None:
        business.orders_rejected_total.labels(str(reason)).inc()

    orders = Orders(
        store,
        app.state.portfolio,
        publisher,
        environment=settings.environment,
        on_placed=lambda o: business.orders_placed_total.labels(o.order_type, o.side).inc(),
        on_rejected=rejected,
    )
    relay = OutboxRelay(
        store,
        publisher,
        publish_timeout_seconds=settings.publish_timeout_seconds,
        on_rejected=rejected,
    )
    app.state.orders = orders

    async def handle(envelope: EventEnvelope, delivery: Delivery) -> None:
        await orders.apply_matching_event(envelope)

    consumer = EventConsumer(
        bootstrap_servers=settings.kafka_bootstrap_servers,
        topics=["matching.v1"],
        group_id=CONSUMER_GROUP,
        service=settings.service_name,
        metrics=metrics,
        handler=handle,
        dead_letters=publisher.publish_raw,
        transient_errors=transient_errors(),
    )
    relaying = asyncio.create_task(relay.run(settings.relay_interval_seconds))
    try:
        while True:
            try:
                await consumer.start()
                break
            except Exception:
                logger.exception("Redpanda consumer not started, retrying in 3 s")
                await asyncio.sleep(3)
        await consumer.run()
    finally:
        relaying.cancel()
        await consumer.stop()
        await publisher.stop()


def create_app(
    settings: Settings | None = None,
    readiness_checks: Sequence[ReadinessCheck] | None = None,
    orders: Orders | None = None,
) -> FastAPI:
    """`orders` is for tests: it is served as is, and no background start-up runs."""
    settings = settings or Settings()
    configure_logging(settings.service_name, settings.log_level)
    metrics = Metrics(settings.service_name)
    business_metrics = register_business_metrics(metrics.registry)
    checks = list(readiness_checks) if readiness_checks is not None else default_checks(settings)
    breaker = CircuitBreaker(
        failure_threshold=settings.breaker_failure_threshold,
        open_seconds=settings.breaker_open_seconds,
        on_state_change=lambda state: business_metrics.breaker_state("portfolio-service", state),
        on_trip=business_metrics.circuit_breaker_trips_total.labels("portfolio-service").inc,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with httpx.AsyncClient() as client:
            app.state.portfolio = HttpPortfolio(
                settings.portfolio_url,
                client,
                breaker,
                timeout_seconds=settings.reservation_timeout_seconds,
            )
            start = orders is None and settings.start_consumers
            task = asyncio.create_task(run_service(app, settings, metrics)) if start else None
            yield
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(
        title=settings.service_name,
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.metrics = metrics
    app.state.business_metrics = business_metrics
    app.state.breaker = breaker
    app.state.orders = orders

    app.add_middleware(
        ObservabilityMiddleware,
        service=settings.service_name,
        metrics=metrics,
        issue_correlation_id=False,
    )
    install_exception_handlers(app)
    app.include_router(health_router(settings.service_name, checks))
    app.include_router(metrics_router(metrics))
    app.include_router(orders_api.router)
    return app
