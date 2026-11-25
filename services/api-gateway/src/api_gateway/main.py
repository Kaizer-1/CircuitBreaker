"""api-gateway app factory.

Run with: uvicorn api_gateway.main:create_app --factory.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from api_gateway import log_redaction
from api_gateway.api import internal_block, proxied, sessions, system_status, ws_proxy
from api_gateway.api.auth import install_problem_handler
from api_gateway.business_metrics import register_business_metrics
from api_gateway.config import Settings
from api_gateway.domain.nicknames import NicknameBlocklist
from api_gateway.domain.rate_limit import RateLimiter
from api_gateway.domain.sessions import Sessions
from api_gateway.runtime.health import ReadinessCheck, health_router
from api_gateway.runtime.jsonlog import configure_logging
from api_gateway.runtime.metrics import Metrics, metrics_router, watch_event_loop
from api_gateway.runtime.middleware import ObservabilityMiddleware
from api_gateway.runtime.problem import install_exception_handlers

# Idle connections are dropped after 2 s, before uvicorn closes them (after 5 s): otherwise a
# request can be sent on a connection the server is closing (httpx.ReadError, a 503 to the caller).
KEEP_ALIVE = httpx.Limits(keepalive_expiry=2.0)


def create_app(
    settings: Settings | None = None,
    readiness_checks: Sequence[ReadinessCheck] | None = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.service_name, settings.log_level)
    log_redaction.install()  # uvicorn logs WebSocket URLs, which carry ?access_token=
    metrics = Metrics(settings.service_name)
    # The gateway has no database or broker of its own, so "ready" means "configured and running".
    checks = list(readiness_checks) if readiness_checks is not None else []

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with httpx.AsyncClient(
            timeout=settings.upstream_timeout_seconds,
            transport=http_transport,
            limits=KEEP_ALIVE,
        ) as client:
            app.state.http_client = client
            watcher = asyncio.create_task(watch_event_loop(metrics))
            yield
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher

    app = FastAPI(
        title=settings.service_name,
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.metrics = metrics
    app.state.business_metrics = register_business_metrics(metrics.registry)
    for upstream in settings.service_upstreams:  # from 0, so rate() sees the first connection
        app.state.business_metrics.upstream_connections_opened.labels(upstream)
    app.state.sessions = Sessions(
        settings.jwt_secret.get_secret_value(), hours=settings.session_hours
    )
    app.state.rate_limiter = RateLimiter()
    app.state.nickname_blocklist = NicknameBlocklist.default()

    # The gateway issues the correlation ID; every other service passes it through.
    app.add_middleware(
        ObservabilityMiddleware,
        service=settings.service_name,
        metrics=metrics,
        issue_correlation_id=True,
    )
    install_exception_handlers(app)
    install_problem_handler(app)
    app.include_router(health_router(settings.service_name, checks))
    app.include_router(metrics_router(metrics))
    app.include_router(system_status.router)
    app.include_router(internal_block.router)
    app.include_router(sessions.router)
    app.include_router(proxied.router)
    app.include_router(ws_proxy.router)
    return app
