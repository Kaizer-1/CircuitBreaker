"""sre-agent app factory.

Run with: uvicorn sre_agent.main:create_app --factory.

Start-up runs in the background and retries until Cassandra answers (like order-service): until
then the API answers 503 and /readyz says "starting". Then the loop reads the alerts of its own
namespace every 5 s, and each incident runs in its own task (application/agent.py).

On Kubernetes the agent acts through the API with its ServiceAccount; in Docker Compose there is
no Kubernetes and no Prometheus, so the loop does not run and the platform is a dry run.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import FastAPI

from sre_agent.adapters.cassandra_store import CassandraStore, connect_cassandra
from sre_agent.adapters.events_kafka import KafkaIncidentSink
from sre_agent.adapters.gitops import ArgoGitOps, GitHubReverter
from sre_agent.adapters.kubernetes import ACCOUNT, DryRunPlatform, KubernetesPlatform
from sre_agent.adapters.loki import LokiLogs
from sre_agent.adapters.openrouter import OpenRouterClient
from sre_agent.adapters.prometheus import PrometheusMetrics
from sre_agent.api import incidents
from sre_agent.application.agent import Agent, AgentConfig
from sre_agent.application.collector import EvidenceCollector
from sre_agent.application.decider import Decider
from sre_agent.application.feed import FeedHub
from sre_agent.application.vitals import VitalsPublisher
from sre_agent.business_metrics import register_business_metrics
from sre_agent.config import Settings
from sre_agent.ports.gitops import GitOps, NoGitOps
from sre_agent.ports.observability import Alert
from sre_agent.ports.platform import Platform
from sre_agent.ports.store import Store
from sre_agent.runtime.checks import cassandra_check, redpanda_check
from sre_agent.runtime.health import ReadinessCheck, health_router
from sre_agent.runtime.jsonlog import configure_logging
from sre_agent.runtime.kafka import EventPublisher
from sre_agent.runtime.metrics import Metrics, metrics_router
from sre_agent.runtime.middleware import ObservabilityMiddleware
from sre_agent.runtime.problem import install_exception_handlers

# Topics this service produces or consumes, per the event catalog.
TOPICS = ("incidents.v1",)
KEEP_ALIVE = httpx.Limits(keepalive_expiry=2.0)  # under uvicorn's 5 s (see order-service)

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


def started_check(app: FastAPI) -> ReadinessCheck:
    async def probe() -> None:
        if app.state.agent is None:
            raise RuntimeError("starting: the incident store is not ready yet")

    return ReadinessCheck("started", probe)


class NoAlerts:
    """Docker Compose: no Prometheus, so no alerts (the loop is not started)."""

    async def firing_alerts(self) -> list[Alert]:
        return []

    async def value(self, query: str) -> float | None:
        return None

    async def by_label(self, query: str, label: str) -> dict[str, float]:
        return {}

    async def series(self, query: str, minutes: int, step_seconds: int) -> list[float | None]:
        return []

    async def series_by_label(
        self, query: str, label: str, minutes: int, step_seconds: int
    ) -> dict[str, list[float | None]]:
        return {}


def now() -> datetime:
    return datetime.now(UTC)


async def start_publisher(publisher: EventPublisher, sink: KafkaIncidentSink) -> None:
    while True:
        try:
            await publisher.start()
            await sink.started()
            return
        except Exception:
            logger.exception("Redpanda not reachable, retrying in 3 s")
            await publisher.stop()
            await asyncio.sleep(3)


async def run_service(app: FastAPI, settings: Settings, http: httpx.AsyncClient) -> None:
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
            store = CassandraStore(session)
            await store.migrate()
            break
        except Exception:
            logger.exception("start-up failed, retrying in 3 s")
            await asyncio.sleep(3)

    publisher = EventPublisher(
        settings.kafka_bootstrap_servers, settings.service_name, app.state.metrics
    )
    sink = KafkaIncidentSink(publisher)
    tasks: list[asyncio.Task[Any]] = [asyncio.create_task(start_publisher(publisher, sink))]
    on_kubernetes = (ACCOUNT / "token").exists()
    platform: Platform = KubernetesPlatform.in_cluster() if on_kubernetes else DryRunPlatform()
    namespace = settings.environment
    prometheus = (
        PrometheusMetrics(http, settings.prometheus_url, namespace)
        if settings.prometheus_url
        else None
    )
    logs = LokiLogs(http, settings.loki_url, namespace) if settings.loki_url else None
    llm = None
    if settings.llm_key:
        fallbacks = tuple(
            m.strip() for m in settings.sre_agent_fallback_models.split(",") if m.strip()
        )
        llm = OpenRouterClient(
            http,
            settings.llm_key,
            settings.sre_agent_model,
            fallback_models=fallbacks,
            base_url=settings.openrouter_url,
            metrics=business,
            ignore_providers=tuple(
                x.strip() for x in settings.sre_agent_ignore_providers.split(",") if x.strip()
            ),
        )
        tasks.append(asyncio.create_task(llm.learn_parameters()))
    else:
        logger.warning("no OpenRouter key: the rule-based fallback decides (ADR-0021)")
    gitops: GitOps = NoGitOps()
    if settings.argocd_url and settings.argo_token:
        github = settings.github_app
        reverter = GitHubReverter(http, settings.github_repository, *github) if github else None
        gitops = ArgoGitOps(http, settings.argocd_url, settings.argo_token, namespace, reverter)
        if reverter is None:
            logger.warning("no GitHub App: a Git-delivered bad release needs a manual revert")
    alerts = prometheus or NoAlerts()
    feed = FeedHub()
    agent = Agent(
        AgentConfig(namespace, settings.approval_default, poll_seconds=settings.poll_seconds),
        alerts=alerts,
        collector=EvidenceCollector(namespace, alerts, logs, platform, now),
        decider=Decider(llm, now),
        platform=platform,
        gitops=gitops,
        store=store,
        sink=sink,
        feed=feed,
        now=now,
        metrics=business,
    )
    await agent.start()
    app.state.store = store
    app.state.agent = agent
    if prometheus is not None and settings.start_agent:
        tasks.append(asyncio.create_task(agent.run()))
        # For screens only: the publisher writes to the feed, which the agent never reads.
        vitals = VitalsPublisher(prometheus, feed, namespace, now, settings.poll_seconds)
        app.state.vitals = vitals
        tasks.append(asyncio.create_task(vitals.run()))
        logger.info("agent started", extra={"namespace": namespace, "model": agent.model})
    else:
        logger.info("no Prometheus: the agent loop does not run (Docker Compose)")
    try:
        await asyncio.Event().wait()  # until the app stops
    finally:
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await agent.stop()
        await publisher.stop()
        session.cluster.shutdown()


def create_app(
    settings: Settings | None = None,
    readiness_checks: Sequence[ReadinessCheck] | None = None,
    agent: Agent | None = None,
    store: Store | None = None,
    start: Callable[[FastAPI, Settings, httpx.AsyncClient], Any] | None = None,
) -> FastAPI:
    """`agent` and `store` are for tests: they are served as they are, nothing else starts."""
    settings = settings or Settings()
    configure_logging(settings.service_name, settings.log_level)
    # httpx logs every request at INFO: the 5-second alert poll alone would be ~17,000 lines a day.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    metrics = Metrics(settings.service_name)
    business_metrics = register_business_metrics(metrics.registry)
    checks = list(readiness_checks) if readiness_checks is not None else default_checks(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if agent is not None:
            app.state.agent, app.state.store = agent, store
            yield
            return
        async with httpx.AsyncClient(timeout=10.0, limits=KEEP_ALIVE) as http:
            task = asyncio.create_task((start or run_service)(app, settings, http))
            yield
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
    app.state.agent = None
    app.state.vitals = None
    app.state.store = None
    if readiness_checks is None:
        checks.append(started_check(app))

    app.add_middleware(
        ObservabilityMiddleware,
        service=settings.service_name,
        metrics=metrics,
        issue_correlation_id=False,
    )
    install_exception_handlers(app)
    app.include_router(health_router(settings.service_name, checks))
    app.include_router(metrics_router(metrics))
    app.include_router(incidents.router)
    return app
