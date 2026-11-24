"""One ASGI middleware for every request: correlation ID, metrics, access log, last-resort 500."""

import logging
import re
import time
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from api_gateway.runtime.jsonlog import correlation_id_var
from api_gateway.runtime.metrics import Metrics
from api_gateway.runtime.problem import problem_code_var, problem_response

CORRELATION_HEADER = "X-Correlation-Id"
_VALID_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")  # rejects anything that could forge a log line

logger = logging.getLogger("access")


class ObservabilityMiddleware:
    """Sets the correlation ID (the gateway issues it; services pass it through), counts and times
    each request, writes one JSON access line, and turns an unexpected exception into a 500."""

    def __init__(
        self, app: ASGIApp, *, service: str, metrics: Metrics, issue_correlation_id: bool = False
    ) -> None:
        self.app = app
        self.service = service
        self.metrics = metrics
        self.issue = issue_correlation_id

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = Headers(scope=scope).get(CORRELATION_HEADER.lower())
        if self.issue or incoming is None or not _VALID_ID.match(incoming):
            correlation_id = str(uuid4())
        else:
            correlation_id = incoming
        token = correlation_id_var.set(correlation_id)
        problem_code: list[str] = []
        problem_token = problem_code_var.set(problem_code)

        method = scope["method"]
        status = 500
        started = False
        began = time.perf_counter()
        self.metrics.http_requests_in_flight.labels(self.service).inc()

        async def send_with_header(message: Message) -> None:
            nonlocal status, started
            if message["type"] == "http.response.start":
                started = True
                status = message["status"]
                MutableHeaders(scope=message)[CORRELATION_HEADER] = correlation_id
            await send(message)

        try:
            try:
                await self.app(scope, receive, send_with_header)
            except Exception:
                logger.exception("unhandled error", extra={"method": method, "path": scope["path"]})
                if started:
                    raise
                response = problem_response(
                    500,
                    "INTERNAL_ERROR",
                    "Internal server error",
                    detail="An unexpected error occurred. Quote the correlation ID when reporting.",
                    instance=scope["path"],
                )
                await response(scope, receive, send_with_header)
        finally:
            elapsed = time.perf_counter() - began
            route = getattr(scope.get("route"), "path", None) or "unmatched"
            self.metrics.http_requests_in_flight.labels(self.service).dec()
            self.metrics.http_requests_total.labels(self.service, route, method, str(status)).inc()
            if problem_code:
                problems = self.metrics.http_problems_total
                problems.labels(self.service, route, str(status), problem_code[0]).inc()
            duration = self.metrics.http_request_duration_seconds
            duration.labels(self.service, route, method).observe(elapsed)
            logger.info(
                "request",
                extra={
                    "method": method,
                    "path": scope["path"],
                    "route": route,
                    "status": status,
                    "duration_ms": round(elapsed * 1000, 2),
                },
            )
            problem_code_var.reset(problem_token)
            correlation_id_var.reset(token)
