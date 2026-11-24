"""A-21 and A-22: chaos-service's "Portfolio database degraded" hook (api/portfolio-service.yaml).

The routes always exist (the contract test compares the spec with the app), but answer 404 unless
the service was started with CHAOS_HOOKS_ENABLED=true; only chaos-service can reach them (network
policy), and the gateway never forwards /internal/ paths.
"""

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from portfolio_service.api.portfolio import _starting
from portfolio_service.application.faults import DatabaseDegraded, FaultAlreadyActiveError
from portfolio_service.runtime.problem import problem_response

router = APIRouter()


class FaultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fault: Literal["DB_DEGRADED"]
    delay_ms: int = Field(ge=100, le=5000)


def _hooks_disabled(request: Request) -> JSONResponse:
    return problem_response(
        404, "NOT_FOUND", "Not found", detail="Chaos hooks are disabled.", instance=request.url.path
    )


def _degraded(request: Request) -> DatabaseDegraded | None:
    degraded: DatabaseDegraded | None = getattr(request.app.state, "database_degraded", None)
    return degraded


@router.post(
    "/internal/v1/faults",
    operation_id="injectDatabaseFault",
    summary="Degrade the portfolio database (chaos hook)",
    openapi_extra={"x-seeds-id": "A-21"},
)
async def inject_fault(request: Request, body: FaultRequest) -> Response:
    if not request.app.state.settings.chaos_hooks_enabled:
        return _hooks_disabled(request)
    degraded = _degraded(request)
    if degraded is None:
        return _starting(request)
    try:
        fault = await degraded.start(body.delay_ms, datetime.now(UTC))
    except FaultAlreadyActiveError as exc:
        return problem_response(
            409,
            "FAULT_ALREADY_ACTIVE",
            "Fault already active",
            detail=f"{exc.fault.fault} has been active since {exc.fault.started_at:%H:%M:%S}.",
            instance=request.url.path,
        )
    started = fault.started_at.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    view = {"fault": fault.fault, "active": True, "delay_ms": fault.delay_ms, "started_at": started}
    return JSONResponse(view, status_code=201)


@router.delete(
    "/internal/v1/faults",
    operation_id="clearDatabaseFault",
    summary="Clear the database fault (chaos hook)",
    openapi_extra={"x-seeds-id": "A-22"},
)
async def clear_fault(request: Request) -> Response:
    if not request.app.state.settings.chaos_hooks_enabled:
        return _hooks_disabled(request)
    degraded = _degraded(request)
    if degraded is None:
        return _starting(request)
    if not await degraded.stop():
        return problem_response(
            404,
            "NO_ACTIVE_FAULT",
            "No active fault",
            detail="There is no fault to clear.",
            instance=request.url.path,
        )
    return Response(status_code=204)
