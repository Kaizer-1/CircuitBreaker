"""A-12 and A-13: chaos-service's memory-leak hook (api/market-data-service.yaml).

The routes always exist (the contract test compares the spec with the app), but answer 404 unless
the service was started with CHAOS_HOOKS_ENABLED=true; only chaos-service can reach them (network
policy), and the gateway never forwards /internal/ paths.
"""

from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from market_data_service.application.faults import FaultAlreadyActiveError, FaultState, MemoryLeak
from market_data_service.runtime.problem import problem_response

router = APIRouter()


class FaultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fault: Literal["MEMORY_LEAK"]
    rate_mb_per_minute: int = Field(ge=1, le=200)


def _view(state: FaultState) -> dict[str, Any]:
    return {
        "fault": state.fault,
        "active": True,
        "rate_mb_per_minute": state.rate_mb_per_minute,
        "started_at": state.started_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }


def _leak(request: Request) -> MemoryLeak | None:
    if not request.app.state.settings.chaos_hooks_enabled:
        return None
    leak: MemoryLeak = request.app.state.memory_leak
    return leak


def _hooks_disabled(request: Request) -> JSONResponse:
    return problem_response(
        404, "NOT_FOUND", "Not found", detail="Chaos hooks are disabled.", instance=request.url.path
    )


@router.post(
    "/internal/v1/faults",
    operation_id="injectFault",
    summary="Inject a fault (chaos hook)",
    openapi_extra={"x-seeds-id": "A-12"},
)
async def inject_fault(request: Request, body: FaultRequest) -> JSONResponse:
    leak = _leak(request)
    if leak is None:
        return _hooks_disabled(request)
    try:
        state = leak.start(body.rate_mb_per_minute, datetime.now(UTC))
    except FaultAlreadyActiveError as exc:
        return problem_response(
            409,
            "FAULT_ALREADY_ACTIVE",
            "Fault already active",
            detail=f"{exc.state.fault} has been active since {exc.state.started_at:%H:%M:%S}.",
            instance=request.url.path,
        )
    return JSONResponse(_view(state), status_code=201)


@router.delete(
    "/internal/v1/faults",
    operation_id="clearFault",
    summary="Clear the active fault (chaos hook)",
    openapi_extra={"x-seeds-id": "A-13"},
)
async def clear_fault(request: Request) -> Response:
    leak = _leak(request)
    if leak is None:
        return _hooks_disabled(request)
    if not await leak.stop():
        return problem_response(
            404,
            "NO_ACTIVE_FAULT",
            "No active fault",
            detail="There is no fault to clear.",
            instance=request.url.path,
        )
    return Response(status_code=204)
