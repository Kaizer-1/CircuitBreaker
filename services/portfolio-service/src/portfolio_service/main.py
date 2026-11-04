"""portfolio-service app factory.

Run with: uvicorn portfolio_service.main:create_app --factory.

On start-up a background task opens the Postgres pool, applies the schema, seeds the bot and load
accounts, then consumes matching.v1 (settlement, A-09) and runs the fail-closed orphan sweeper
(ADR-0017). The HTTP API answers 503 until the database is ready.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from portfolio_service.adapters.order_service_http import HttpOrderService
from portfolio_service.adapters.partitions import partition_for
from portfolio_service.adapters.postgres import PostgresPortfolioStore
from portfolio_service.adapters.postgres_listener import PostgresListener
from portfolio_service.api import portfolio, stubs
from portfolio_service.application.live import PortfolioHub
from portfolio_service.application.portfolio import PortfolioService
from portfolio_service.application.sweeper import OrphanSweeper
from portfolio_service.business_metrics import BusinessMetrics, register_business_metrics
from portfolio_service.config import Settings
from portfolio_service.runtime.checks import postgres_check, redpanda_check
from portfolio_service.runtime.events import EventEnvelope
from portfolio_service.runtime.health import ReadinessCheck, health_router
from portfolio_service.runtime.jsonlog import configure_logging
from portfolio_service.runtime.kafka import (
    TRANSIENT_ERRORS,
    Delivery,
    EventConsumer,
    EventPublisher,
    consumer_check,
)
from portfolio_service.runtime.metrics import Metrics, metrics_router
from portfolio_service.runtime.middleware import ObservabilityMiddleware
from portfolio_service.runtime.problem import install_exception_handlers

# Topics this service produces or consumes, per the event catalog.
TOPICS = ("matching.v1",)
CONSUMER_GROUP = "portfolio-service.matching.v1"

logger = logging.getLogger("startup")
# Idle connections are dropped after 2 s, before uvicorn closes them (after 5 s): otherwise a
# request can be sent on a connection the server is closing (httpx.ReadError, a 503 to the caller).
KEEP_ALIVE = httpx.Limits(keepalive_expiry=2.0)


def default_checks(settings: Settings) -> list[ReadinessCheck]:
    return [
        redpanda_check(settings.kafka_bootstrap_servers, TOPICS),
        postgres_check(
            settings.postgres_host,
            settings.postgres_port,
            settings.postgres_db,
            settings.postgres_user,
            settings.postgres_password.get_secret_value(),
        ),
    ]


def transient_errors() -> tuple[type[BaseException], ...]:
    """Infrastructure errors: retried until they recover, never dead-lettered (runtime/kafka.py)."""
    from aiokafka.errors import KafkaError
    from psycopg import OperationalError
    from psycopg.errors import DeadlockDetected, SerializationFailure
    from psycopg_pool import PoolTimeout

    return (
        *TRANSIENT_ERRORS,
        KafkaError,
        OperationalError,
        DeadlockDetected,
        SerializationFailure,
        PoolTimeout,
    )


def caught_up_on(consumer: EventConsumer) -> Callable[[str], bool]:
    """The sweeper may handle a ticker only if this replica owns its partition and read it all."""

    def can_sweep(ticker: str) -> bool:
        partitions = consumer.partitions_for("matching.v1")
        if not partitions:
            return False
        lag = consumer.lag().get(("matching.v1", partition_for(ticker, partitions)))
        return lag == 0  # None: not our partition, or nothing read yet

    return can_sweep


async def run_service(app: FastAPI, settings: Settings, metrics: Metrics) -> None:
    conninfo = (
        f"host={settings.postgres_host} port={settings.postgres_port} dbname={settings.postgres_db}"
        f" user={settings.postgres_user} password={settings.postgres_password.get_secret_value()}"
        " connect_timeout=5"
    )
    while True:
        try:
            store = await PostgresPortfolioStore.open(
                conninfo, max_size=settings.postgres_pool_size
            )
            await store.migrate()
            service = PortfolioService(store)
            await service.seed_accounts()
            break
        except Exception:
            logger.exception("start-up failed, retrying in 3 s")
            await asyncio.sleep(3)
    app.state.service = service
    business: BusinessMetrics = app.state.business_metrics
    hub = PortfolioHub(service.portfolio)
    business.portfolio_ws_connections.set_function(hub.connections)
    app.state.hub = hub
    listener = PostgresListener(conninfo, hub.on_change, hub.refresh_all)

    async def handle(envelope: EventEnvelope, delivery: Delivery) -> None:
        applied = await service.apply_event(envelope)
        if applied and envelope.event_type == "TradeExecuted":
            business.trades_settled_total.inc()

    publisher = EventPublisher(settings.kafka_bootstrap_servers, settings.service_name, metrics)
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
    app.state.consumer = consumer
    async with httpx.AsyncClient(limits=KEEP_ALIVE) as client:
        sweeper = OrphanSweeper(
            store,
            HttpOrderService(settings.order_service_url, client),
            can_sweep=caught_up_on(consumer),
            orphan_after_seconds=settings.orphan_after_seconds,
            on_requested=business.orphan_expiry_requests_total.inc,
        )
        while True:
            try:
                await publisher.start()
                await consumer.start()
                break
            except Exception:
                logger.exception("Redpanda not reachable, retrying in 3 s")
                await publisher.stop()
                await asyncio.sleep(3)
        background = [
            asyncio.create_task(sweeper.run(settings.sweep_interval_seconds)),
            asyncio.create_task(listener.run()),  # Q-10: every replica hears every change
            asyncio.create_task(hub.run()),  # Q-10: re-value on price moves, once a second
        ]
        try:
            await consumer.run()
        finally:
            for task in background:
                task.cancel()
            await consumer.stop()
            await publisher.stop()
            await store.close()


def create_app(
    settings: Settings | None = None,
    readiness_checks: Sequence[ReadinessCheck] | None = None,
    service: PortfolioService | None = None,
) -> FastAPI:
    """`service` is for tests: it is served as is, and no background start-up runs."""
    settings = settings or Settings()
    configure_logging(settings.service_name, settings.log_level)
    metrics = Metrics(settings.service_name)
    business_metrics = register_business_metrics(metrics.registry)
    checks = list(readiness_checks) if readiness_checks is not None else default_checks(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        start = service is None and settings.start_consumers
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
    app.state.service = service
    app.state.hub = PortfolioHub(service.portfolio) if service is not None else None
    app.state.consumer = None
    if readiness_checks is None:
        checks.append(consumer_check(lambda: app.state.consumer))

    app.add_middleware(
        ObservabilityMiddleware,
        service=settings.service_name,
        metrics=metrics,
        issue_correlation_id=False,
    )
    install_exception_handlers(app)
    app.include_router(health_router(settings.service_name, checks))
    app.include_router(metrics_router(metrics))
    app.include_router(portfolio.router)
    app.include_router(stubs.router)
    return app
