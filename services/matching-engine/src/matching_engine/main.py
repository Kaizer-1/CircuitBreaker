"""matching-engine app factory.

Run with: uvicorn matching_engine.main:create_app --factory.

On start-up a background task connects to Cassandra, applies the schema, rebuilds every order book
purely from the event store (latest snapshot + later events), publishes anything not yet published,
and only then starts consuming orders.v1. /readyz stays 503 until the books are rebuilt.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from fastapi import FastAPI

from matching_engine.adapters.cassandra_store import CassandraEventStore, connect_cassandra
from matching_engine.adapters.redpanda import KafkaMarketPublisher
from matching_engine.api import event_store
from matching_engine.application.engine import MatchingEngine
from matching_engine.business_metrics import BusinessMetrics, register_business_metrics
from matching_engine.config import Settings
from matching_engine.domain.model import Decision, TradeExecuted
from matching_engine.runtime.checks import cassandra_check, redpanda_check
from matching_engine.runtime.events import EventEnvelope
from matching_engine.runtime.health import ReadinessCheck, health_router
from matching_engine.runtime.jsonlog import configure_logging
from matching_engine.runtime.kafka import TRANSIENT_ERRORS, Delivery, EventConsumer, EventPublisher
from matching_engine.runtime.metrics import Metrics, metrics_router
from matching_engine.runtime.middleware import ObservabilityMiddleware
from matching_engine.runtime.problem import install_exception_handlers

# Topics this service produces or consumes, per the event catalog.
TOPICS = ("orders.v1", "matching.v1")
CONSUMER_GROUP = "matching-engine.orders.v1"

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


def books_check(app: FastAPI) -> ReadinessCheck:
    async def probe() -> None:
        engine: MatchingEngine | None = getattr(app.state, "engine", None)
        if engine is None or not engine.ready:
            raise RuntimeError("order books are still being rebuilt from the event store")

    return ReadinessCheck("books", probe)


def transient_errors() -> tuple[type[BaseException], ...]:
    """Infrastructure errors: retried until they recover, never dead-lettered (runtime/kafka.py)."""
    from aiokafka.errors import KafkaError
    from cassandra import OperationTimedOut, Timeout, Unavailable
    from cassandra.cluster import NoHostAvailable

    return (*TRANSIENT_ERRORS, KafkaError, OperationTimedOut, Timeout, Unavailable, NoHostAvailable)


def count(metrics: BusinessMetrics, envelope: EventEnvelope, decision: Decision | None) -> None:
    if decision is None:  # a duplicate or an event type the engine ignores
        return
    metrics.commands_processed_total.labels(envelope.event_type, decision.outcome).inc()
    for event in decision.events:
        if isinstance(event, TradeExecuted):
            metrics.trades_executed_total.labels(event.ticker).inc()


async def run_engine(app: FastAPI, settings: Settings, metrics: Metrics) -> None:
    """Connect, rebuild, then consume orders.v1 until cancelled. Retries until the stores answer."""
    publisher = EventPublisher(settings.kafka_bootstrap_servers, settings.service_name, metrics)
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
            store = CassandraEventStore(session)
            await store.migrate()
            await publisher.start()
            engine = MatchingEngine(
                store,
                KafkaMarketPublisher(publisher),
                environment=settings.environment,
                snapshot_every=settings.snapshot_every,
            )
            await engine.recover()
            break
        except Exception:
            logger.exception("start-up failed, retrying in 3 s")
            await publisher.stop()
            await asyncio.sleep(3)
    app.state.engine = engine

    async def handle(envelope: EventEnvelope, delivery: Delivery) -> None:
        count(app.state.business_metrics, envelope, await engine.handle_envelope(envelope))

    consumer = EventConsumer(
        bootstrap_servers=settings.kafka_bootstrap_servers,
        topics=["orders.v1"],
        group_id=CONSUMER_GROUP,
        service=settings.service_name,
        metrics=metrics,
        handler=handle,
        dead_letters=publisher.publish_raw,
        transient_errors=transient_errors(),
    )
    await consumer.start()
    try:
        await consumer.run()
    finally:
        await consumer.stop()
        await publisher.stop()


def create_app(
    settings: Settings | None = None,
    readiness_checks: Sequence[ReadinessCheck] | None = None,
    engine: MatchingEngine | None = None,
) -> FastAPI:
    """`engine` is for tests: it is served as is, and no background start-up runs."""
    settings = settings or Settings()
    configure_logging(settings.service_name, settings.log_level)
    metrics = Metrics(settings.service_name)
    business_metrics = register_business_metrics(metrics.registry)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        start = engine is None and settings.start_consumers
        task = asyncio.create_task(run_engine(app, settings, metrics)) if start else None
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
    app.state.engine = engine
    checks = list(readiness_checks) if readiness_checks is not None else default_checks(settings)
    if readiness_checks is None:
        checks.append(books_check(app))

    app.add_middleware(
        ObservabilityMiddleware,
        service=settings.service_name,
        metrics=metrics,
        issue_correlation_id=False,
    )
    install_exception_handlers(app)
    app.include_router(health_router(settings.service_name, checks))
    app.include_router(metrics_router(metrics))
    app.include_router(event_store.router)
    return app
