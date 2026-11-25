"""chaos-service app factory.

Run with: uvicorn chaos_service.main:create_app --factory.

The lab (application/lab.py) acts on the order-service Deployment through the Kubernetes API when
it runs in a pod with its ServiceAccount (Docker Compose has none: the bad deploy is unavailable
there) and on the three fault hooks over HTTP. A background task reads incidents.v1 to time each
experiment; chaos-service never sends anything to the SRE agent.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from chaos_service.adapters.hooks_http import HttpFaultHooks
from chaos_service.adapters.kubernetes import ACCOUNT, KubernetesOrderService, NoKubernetes
from chaos_service.api import chaos, stubs
from chaos_service.application.lab import ChaosLab
from chaos_service.business_metrics import register_business_metrics
from chaos_service.config import Settings
from chaos_service.ports.targets import OrderServiceDeployment
from chaos_service.runtime.checks import redpanda_check
from chaos_service.runtime.events import EventEnvelope
from chaos_service.runtime.health import ReadinessCheck, health_router
from chaos_service.runtime.jsonlog import configure_logging
from chaos_service.runtime.kafka import Delivery, EventConsumer, EventPublisher, consumer_check
from chaos_service.runtime.metrics import Metrics, metrics_router
from chaos_service.runtime.middleware import ObservabilityMiddleware
from chaos_service.runtime.problem import install_exception_handlers

# Topics this service produces or consumes, per the event catalog.
TOPICS = ("incidents.v1",)
CONSUMER_GROUP = "chaos-service.incidents.v1"

logger = logging.getLogger("startup")
KEEP_ALIVE = httpx.Limits(keepalive_expiry=2.0)  # under uvicorn's 5 s (see order-service)


def default_checks(settings: Settings) -> list[ReadinessCheck]:
    return [
        redpanda_check(settings.kafka_bootstrap_servers, TOPICS),
    ]


async def follow_incidents(app: FastAPI, settings: Settings, metrics: Metrics) -> None:
    lab: ChaosLab = app.state.lab

    async def handle(envelope: EventEnvelope, delivery: Delivery) -> None:
        await lab.on_incident_event(envelope)

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
    app.state.consumer = consumer
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
        await consumer.stop()
        await publisher.stop()


def create_app(
    settings: Settings | None = None,
    readiness_checks: Sequence[ReadinessCheck] | None = None,
    lab: ChaosLab | None = None,
) -> FastAPI:
    """`lab` is for tests: it is served as is, and nothing runs in the background."""
    settings = settings or Settings()
    configure_logging(settings.service_name, settings.log_level)
    metrics = Metrics(settings.service_name)
    business_metrics = register_business_metrics(metrics.registry)
    checks = list(readiness_checks) if readiness_checks is not None else default_checks(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if lab is not None:
            app.state.lab = lab
            yield
            return
        async with httpx.AsyncClient(timeout=10.0, limits=KEEP_ALIVE) as client:
            on_kubernetes = (ACCOUNT / "token").exists()
            deployment: OrderServiceDeployment = (
                KubernetesOrderService.in_cluster() if on_kubernetes else NoKubernetes()
            )
            if not on_kubernetes:
                logger.info("not in Kubernetes: BAD_DEPLOY and the replica reset are unavailable")
            hooks = HttpFaultHooks(
                client,
                market_data_url=settings.market_data_url,
                portfolio_url=settings.portfolio_url,
                bot_traders_url=settings.bot_traders_url,
            )
            app.state.lab = ChaosLab(
                deployment,
                hooks,
                replicas_known=on_kubernetes,
                on_started=business_metrics.started,
                on_ended=business_metrics.ended,
                on_reset=business_metrics.chaos_resets_total.inc,
            )
            task = (
                asyncio.create_task(follow_incidents(app, settings, metrics))
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
    app.include_router(chaos.router)
    app.include_router(stubs.router)
    return app
