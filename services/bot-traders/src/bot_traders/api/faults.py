"""A-23 and A-24: chaos-service's panic-sell hook (api/bot-traders.yaml; ADR-0020).

The routes always exist (the contract test compares the spec with the app), but answer 404 unless
the service was started with CHAOS_HOOKS_ENABLED=true; only chaos-service can reach them (network
policy), and the gateway never forwards /internal/ paths.
"""

from datetime import UTC, datetime
from typing import Literal

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from bot_traders.application.panic import PanicAlreadyRunningError, PanicSell, PanicState
from bot_traders.runtime.problem import problem_response

router = APIRouter()


class PanicRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fault: Literal["PANIC_SELL"]
    requests_per_second: int = Field(ge=10, le=1000)
    duration_seconds: int = Field(ge=10, le=900)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _view(state: PanicState) -> dict[str, object]:
    return {
        "fault": "PANIC_SELL",
        "active": True,
        "requests_per_second": state.requests_per_second,
        "duration_seconds": state.duration_seconds,
        "started_at": _iso(state.started_at),
        "ends_at": _iso(state.ends_at),
    }


def _panic(request: Request) -> PanicSell | None:
    if not request.app.state.settings.chaos_hooks_enabled:
        return None
    panic: PanicSell | None = request.app.state.panic
    return panic


def _hooks_disabled(request: Request) -> JSONResponse:
    return problem_response(
        404, "NOT_FOUND", "Not found", detail="Chaos hooks are disabled.", instance=request.url.path
    )


@router.post(
    "/internal/v1/faults",
    operation_id="startPanicSell",
    summary="Start a panic sell (chaos hook)",
    openapi_extra={"x-seeds-id": "A-23"},
)
async def start_panic(request: Request, body: PanicRequest) -> JSONResponse:
    panic = _panic(request)
    if panic is None:
        return _hooks_disabled(request)
    try:
        state = await panic.start(
            body.requests_per_second, body.duration_seconds, datetime.now(UTC)
        )
    except PanicAlreadyRunningError as exc:
        return problem_response(
            409,
            "FAULT_ALREADY_ACTIVE",
            "Fault already active",
            detail=f"PANIC_SELL has been active since {exc.state.started_at:%H:%M:%S}.",
            instance=request.url.path,
        )
    except httpx.HTTPError as exc:  # the bots could not sign in (A-03)
        return problem_response(
            503,
            "DEPENDENCY_UNAVAILABLE",
            "Temporarily unavailable",
            detail=f"The bots could not sign in through api-gateway: {type(exc).__name__}.",
            instance=request.url.path,
        )
    return JSONResponse(_view(state), status_code=201)


@router.delete(
    "/internal/v1/faults",
    operation_id="stopPanicSell",
    summary="Stop the panic sell (chaos hook)",
    openapi_extra={"x-seeds-id": "A-24"},
)
async def stop_panic(request: Request) -> Response:
    panic = _panic(request)
    if panic is None:
        return _hooks_disabled(request)
    if not await panic.stop():
        return problem_response(
            404,
            "NO_ACTIVE_FAULT",
            "No active fault",
            detail="There is no panic sell to stop.",
            instance=request.url.path,
        )
    return Response(status_code=204)
