"""api-gateway app factory.

Run with: uvicorn api_gateway.main:create_app --factory.
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from api_gateway.api import internal_block, stubs, system_status
from api_gateway.config import Settings
from api_gateway.runtime.health import ReadinessCheck, health_router
from api_gateway.runtime.jsonlog import configure_logging
from api_gateway.runtime.metrics import Metrics, metrics_router
from api_gateway.runtime.middleware import ObservabilityMiddleware
from api_gateway.runtime.problem import install_exception_handlers


def create_app(
    settings: Settings | None = None,
    readiness_checks: Sequence[ReadinessCheck] | None = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.service_name, settings.log_level)
    metrics = Metrics(settings.service_name)
    # The gateway has no database or broker of its own, so "ready" means "configured and running".
    checks = list(readiness_checks) if readiness_checks is not None else []

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with httpx.AsyncClient(
            timeout=settings.upstream_timeout_seconds, transport=http_transport
        ) as client:
            app.state.http_client = client
            yield

    app = FastAPI(
        title=settings.service_name,
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.metrics = metrics

    # The gateway issues the correlation ID; every other service passes it through.
    app.add_middleware(
        ObservabilityMiddleware,
        service=settings.service_name,
        metrics=metrics,
        issue_correlation_id=True,
    )
    install_exception_handlers(app)
    app.include_router(health_router(settings.service_name, checks))
    app.include_router(metrics_router(metrics))
    app.include_router(system_status.router)
    app.include_router(internal_block.router)
    app.include_router(stubs.router)
    return app
