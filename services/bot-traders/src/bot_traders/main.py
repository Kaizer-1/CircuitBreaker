"""bot-traders app factory.

Run with: uvicorn bot_traders.main:create_app --factory.

With BOTS_ENABLED=false (the default) the service only answers /healthz, /readyz and /metrics:
nothing trades until `make bots`. With it on, the fleet signs in its 20 bots and acts on every
CandleClosed from candles.v1.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from bot_traders.adapters.gateway import GatewayExchange
from bot_traders.api import stubs
from bot_traders.application.fleet import Fleet
from bot_traders.business_metrics import register_business_metrics
from bot_traders.config import Settings
from bot_traders.runtime.checks import redpanda_check
from bot_traders.runtime.events import EventEnvelope
from bot_traders.runtime.health import ReadinessCheck, health_router
from bot_traders.runtime.jsonlog import configure_logging
from bot_traders.runtime.kafka import Delivery, EventConsumer, EventPublisher
from bot_traders.runtime.metrics import Metrics, metrics_router
from bot_traders.runtime.middleware import ObservabilityMiddleware
from bot_traders.runtime.problem import install_exception_handlers

logger = logging.getLogger("bot-traders")
# Idle connections are dropped after 2 s, before uvicorn closes them (after 5 s): otherwise a
# request can be sent on a connection the server is closing (httpx.ReadError, a 503 to the caller).
KEEP_ALIVE = httpx.Limits(keepalive_expiry=2.0)

# Topics this service produces or consumes, per the event catalog.
TOPICS = ("candles.v1",)
CONSUMER_GROUP = "bot-traders.candles.v1"


def default_checks(settings: Settings) -> list[ReadinessCheck]:
    return [
        redpanda_check(settings.kafka_bootstrap_servers, TOPICS),
    ]


def fleet_check(app: FastAPI) -> ReadinessCheck:
    """With bots on: ready once every bot has signed in and the market makers have quoted."""

    async def probe() -> None:
        fleet: Fleet | None = app.state.fleet
        if fleet is None or not fleet.started:
            raise RuntimeError("the bots have not signed in yet")

    return ReadinessCheck("bots", probe)


async def run_bots(app: FastAPI, settings: Settings, metrics: Metrics) -> None:
    business = app.state.business_metrics
    async with httpx.AsyncClient(timeout=5.0, limits=KEEP_ALIVE) as client:
        exchange = GatewayExchange(
            settings.gateway_url, settings.bot_secret.get_secret_value(), client
        )
        fleet = Fleet(
            exchange,
            seed=settings.bot_random_seed,
            activity=settings.bot_activity,
            interval_seconds=settings.candle_interval_seconds,
            on_order=lambda strategy, result, ticker: business.bot_orders_total.labels(
                strategy, result
            ).inc(),
            on_timing=business.timing,
        )
        app.state.fleet = fleet
        while True:
            try:
                await fleet.start()
                break
            except Exception:
                logger.exception("bots could not sign in (is the gateway up?), retrying in 3 s")
                await asyncio.sleep(3)
        business.bots_active.set(len(fleet.bots))

        async def handle(envelope: EventEnvelope, delivery: Delivery) -> None:
            await fleet.on_candle(envelope)

        publisher = EventPublisher(settings.kafka_bootstrap_servers, settings.service_name, metrics)
        consumer = EventConsumer(
            bootstrap_servers=settings.kafka_bootstrap_servers,
            topics=list(TOPICS),
            group_id=CONSUMER_GROUP,
            service=settings.service_name,
            metrics=metrics,
            handler=handle,
            dead_letters=publisher.publish_raw,
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
        try:
            await consumer.run()
        finally:
            fleet.stop()
            business.bots_active.set(0)
            await consumer.stop()
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
        start = settings.bots_enabled and settings.start_consumers
        task = asyncio.create_task(run_bots(app, settings, metrics)) if start else None
        if not settings.bots_enabled:
            logger.info("bots are off (BOTS_ENABLED=false): `make bots` turns them on")
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
    app.state.fleet = None
    if readiness_checks is None and settings.bots_enabled:
        checks.append(fleet_check(app))

    app.add_middleware(
        ObservabilityMiddleware,
        service=settings.service_name,
        metrics=metrics,
        issue_correlation_id=False,
    )
    install_exception_handlers(app)
    app.include_router(health_router(settings.service_name, checks))
    app.include_router(metrics_router(metrics))
    app.include_router(stubs.router)
    return app
