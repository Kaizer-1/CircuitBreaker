"""portfolio-service app factory.

Run with: uvicorn portfolio_service.main:create_app --factory.
"""

from collections.abc import Sequence

from fastapi import FastAPI

from portfolio_service.api import stubs
from portfolio_service.business_metrics import register_business_metrics
from portfolio_service.config import Settings
from portfolio_service.runtime.checks import postgres_check, redpanda_check
from portfolio_service.runtime.health import ReadinessCheck, health_router
from portfolio_service.runtime.jsonlog import configure_logging
from portfolio_service.runtime.metrics import Metrics, metrics_router
from portfolio_service.runtime.middleware import ObservabilityMiddleware
from portfolio_service.runtime.problem import install_exception_handlers

# Topics this service produces or consumes, per the event catalog.
TOPICS = ("matching.v1",)


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
