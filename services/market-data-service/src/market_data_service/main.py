"""market-data-service app factory.

Run with: uvicorn market_data_service.main:create_app --factory.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from market_data_service.adapters.cassandra_store import CassandraMarketStore, connect_cassandra
from market_data_service.adapters.engine_http import HttpEventStore
from market_data_service.adapters.matching_feed import MatchingFeed
from market_data_service.api import market as market_api
from market_data_service.api import stubs
from market_data_service.application.hub import MarketHub
from market_data_service.application.projection import MarketProjection
from market_data_service.business_metrics import register_business_metrics
from market_data_service.config import Settings
from market_data_service.runtime.checks import cassandra_check, redpanda_check
from market_data_service.runtime.health import ReadinessCheck, health_router
from market_data_service.runtime.jsonlog import configure_logging
from market_data_service.runtime.kafka import EventPublisher
from market_data_service.runtime.metrics import Metrics, metrics_router
from market_data_service.runtime.middleware import ObservabilityMiddleware
from market_data_service.runtime.problem import install_exception_handlers

logger = logging.getLogger("market-data-service")
# Idle connections are dropped after 2 s, before uvicorn closes them (after 5 s): otherwise a
# request can be sent on a connection the server is closing (httpx.ReadError, a 503 to the caller).
KEEP_ALIVE = httpx.Limits(keepalive_expiry=2.0)

# Topics this service produces or consumes, per the event catalog.
TOPICS = ("matching.v1", "candles.v1")


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


def projection_check(app: FastAPI) -> ReadinessCheck:
    """Ready once the market is rebuilt from the event store and the live feed has reached the
    end of every partition (coding standards section 7: "consumers are caught up")."""

    async def probe() -> None:
        projection, feed = app.state.projection, app.state.feed
        if projection is None or not projection.ready:
            raise RuntimeError("the market is still being rebuilt from the event store")
        if feed is None or not feed.caught_up_once:
            raise RuntimeError("matching.v1 has not been read to the end yet")

    return ReadinessCheck("projection", probe)


async def run_service(app: FastAPI, settings: Settings, metrics: Metrics) -> None:
    business = app.state.business_metrics
    publisher = EventPublisher(settings.kafka_bootstrap_servers, settings.service_name, metrics)
    feed = MatchingFeed(settings.kafka_bootstrap_servers, settings.service_name, metrics)
    async with httpx.AsyncClient(limits=KEEP_ALIVE) as client:
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
                store = CassandraMarketStore(session)
                await store.migrate()
                await publisher.start()
                # The feed is at the end of every partition before the catch-up starts.
                await feed.start()
                projection = MarketProjection(
                    store,
                    HttpEventStore(settings.matching_engine_url, client),
                    publisher,
                    environment=settings.environment,
                    feed_caught_up=feed.caught_up,
                    on_gap=business.projection_gaps_filled_total.labels(settings.service_name).inc,
                    on_closed=lambda interval: business.candles_closed_total.labels(
                        settings.service_name, interval
                    ).inc(),
                )
                await projection.start()
                break
            except Exception:
                logger.exception("start-up failed, retrying in 3 s")
                await feed.stop()
                await publisher.stop()
                await asyncio.sleep(3)

        hub = MarketHub(projection)
        business.market_ws_connections.labels(settings.service_name).set_function(hub.connections)
        app.state.store, app.state.projection, app.state.hub = store, projection, hub
        app.state.feed = feed
        tasks = [
            asyncio.create_task(projection.run_clock()),
            asyncio.create_task(hub.run()),
        ]
        try:
            await feed.run(projection.handle)
        finally:
            for task in tasks:
                task.cancel()
            await feed.stop()
            await publisher.stop()


def create_app(
    settings: Settings | None = None, readiness_checks: Sequence[ReadinessCheck] | None = None
) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.service_name, settings.log_level)
    metrics = Metrics(settings.service_name)
    business_metrics = register_business_metrics(metrics.registry)
    checks = list(readiness_checks) if readiness_checks is not None else default_checks(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        task = (
            asyncio.create_task(run_service(app, settings, metrics))
            if settings.start_consumers
            else None
        )
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
    app.state.store = None
    app.state.projection = None
    app.state.hub = None
    app.state.feed = None
    if readiness_checks is None:
        checks.append(projection_check(app))

    app.add_middleware(
        ObservabilityMiddleware,
        service=settings.service_name,
        metrics=metrics,
        issue_correlation_id=False,
    )
    install_exception_handlers(app)
    app.include_router(health_router(settings.service_name, checks))
    app.include_router(metrics_router(metrics))
    app.include_router(market_api.router)
    app.include_router(stubs.router)
    return app
