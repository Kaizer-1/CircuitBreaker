"""Authentication, authorisation and rate limiting for every public route.

Each route declares who may call it (api/api-gateway.yaml, "Roles: ..."). The dependency verifies
the bearer token, checks the role, applies the caller's rate limit, and returns the Principal the
proxy turns into X-Trader-Id / X-Role / X-Nickname.
"""

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from api_gateway.domain.rate_limit import (
    ANONYMOUS_PER_SECOND,
    BOT_PER_SECOND,
    TRADER_PER_SECOND,
    RateLimiter,
)
from api_gateway.domain.sessions import InvalidTokenError, Principal, Role, Sessions
from api_gateway.runtime.problem import problem_response


class ApiProblemError(Exception):
    def __init__(
        self, status: int, code: str, title: str, detail: str, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(detail)
        self.status, self.code, self.title, self.detail = status, code, title, detail
        self.headers = headers or {}


def install_problem_handler(app: FastAPI) -> None:
    @app.exception_handler(ApiProblemError)
    async def _problem(request: Request, exc: ApiProblemError) -> JSONResponse:
        response = problem_response(
            exc.status, exc.code, exc.title, detail=exc.detail, instance=request.url.path
        )
        response.headers.update(exc.headers)
        return response


def _too_many(retry_after: int, detail: str) -> ApiProblemError:
    return ApiProblemError(
        429, "RATE_LIMITED", "Slow down", detail, {"Retry-After": str(retry_after)}
    )


def require(*roles: Role) -> Callable[[Request], Awaitable[Principal]]:
    """A dependency: the caller must hold a valid token, with one of `roles` (any role if none)."""

    async def dependency(request: Request) -> Principal:
        sessions: Sessions = request.app.state.sessions
        limiter: RateLimiter = request.app.state.rate_limiter
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise ApiProblemError(
                401, "UNAUTHENTICATED", "Not signed in", "The access token is missing. Join first."
            )
        try:
            principal = sessions.verify(token)
        except InvalidTokenError:
            raise ApiProblemError(
                401,
                "UNAUTHENTICATED",
                "Not signed in",
                "The access token is invalid or has expired. Join again to get a new one.",
            ) from None
        if roles and principal.role not in roles:
            needed = " or ".join(r.value for r in roles)
            raise ApiProblemError(
                403, "FORBIDDEN", "Not allowed", f"This action requires the {needed} role."
            )
        rate = BOT_PER_SECOND if principal.role is Role.BOT else TRADER_PER_SECOND
        wait = limiter.check(f"{principal.role}:{principal.sub}", rate)
        if wait is not None:
            raise _too_many(wait, f"Limit is {rate} requests per second per {principal.role}.")
        return principal

    return dependency


async def limit_by_ip(request: Request) -> None:
    """The session endpoints have no token yet: limit them per client address."""
    limiter: RateLimiter = request.app.state.rate_limiter
    address = request.client.host if request.client else "unknown"
    wait = limiter.check(f"ip:{address}", ANONYMOUS_PER_SECOND)
    if wait is not None:
        raise _too_many(wait, f"Limit is {ANONYMOUS_PER_SECOND} requests per second per address.")


ANY_ROLE = require()
TRADING = require(Role.TRADER, Role.BOT)
ADMIN = require(Role.ADMIN)
