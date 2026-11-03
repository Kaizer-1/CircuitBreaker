"""sre-agent app factory.

Run with: uvicorn sre_agent.main:create_app --factory.
"""

from collections.abc import Sequence

from fastapi import FastAPI

from sre_agent.api import stubs
from sre_agent.business_metrics import register_business_metrics
from sre_agent.config import Settings
from sre_agent.runtime.checks import cassandra_check, redpanda_check
from sre_agent.runtime.health import ReadinessCheck, health_router
from sre_agent.runtime.jsonlog import configure_logging
from sre_agent.runtime.metrics import Metrics, metrics_router
from sre_agent.runtime.middleware import ObservabilityMiddleware
from sre_agent.runtime.problem import install_exception_handlers

# Topics this service produces or consumes, per the event catalog.
TOPICS = ("incidents.v1",)


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


def create_app(
    settings: Settings | None = None, readiness_checks: Sequence[ReadinessCheck] | None = None
) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.service_name, settings.log_level)
    metrics = Metrics(settings.service_name)
    business_metrics = register_business_metrics(metrics.registry)
    checks = list(readiness_checks) if readiness_checks is not None else default_checks(settings)

    app = FastAPI(title=settings.service_name, version="0.1.0", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.metrics = metrics
    app.state.business_metrics = business_metrics

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
