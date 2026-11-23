"""Q-12 time travel (api/market-data-service.yaml).

A thin router: check the request, take a place (application/slots.py), ask the use case
(application/time_travel.py) and shape the answer as TimeTravelView.
"""

import logging
from datetime import datetime
from typing import Annotated

import httpx
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from market_data_service.api.market import Ticker, _projection, _starting
from market_data_service.application.slots import Slots
from market_data_service.application.time_travel import HORIZON_MS, TimeTravel
from market_data_service.application.views import time_travel_view
from market_data_service.runtime.problem import problem_response

logger = logging.getLogger("time-travel")
# A viewer's clock may run slightly ahead of this service's: a moment up to this far ahead counts
# as now (so "rewind to now" always lands after the latest event and can be verified).
CLOCK_SKEW_MS = 2_000

router = APIRouter()


def is_admin(role: str | None) -> bool:
    """The gateway sets X-Role from the verified token (and strips any client copy)."""
    return role == "admin"


def at_ms(value: str) -> int:
    """An ISO-8601 time with an offset (`Z` or `+05:30`), to milliseconds since the epoch.
    A time without an offset is refused: it would silently mean the server's local time."""
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("no UTC offset")
    return int(moment.timestamp() * 1000)


@router.get(
    "/v1/time-travel/{ticker}",
    operation_id="getMarketAt",
    summary="Get the market as it was at time T",
    openapi_extra={"x-seeds-id": "Q-12"},
)
async def get_market_at(
    request: Request,
    ticker: Ticker,
    at: str,
    depth: Annotated[int, Query(ge=1, le=50)] = 10,
) -> JSONResponse:
    time_travel: TimeTravel | None = getattr(request.app.state, "time_travel", None)
    projection = _projection(request)
    if time_travel is None or projection is None:
        return _starting(request)
    try:
        moment = at_ms(at)
    except ValueError:
        return problem_response(
            422,
            "VALIDATION_FAILED",
            "Validation failed",
            detail="`at` must be an ISO-8601 time with a UTC offset, e.g. 2026-11-20T10:42:15Z.",
            instance=request.url.path,
        )
    now = projection.now_ms()
    if moment > now + CLOCK_SKEW_MS:
        return problem_response(
            422,
            "TIME_IN_FUTURE",
            "That moment hasn't happened yet",
            detail=f"at={at} is in the future.",
            instance=request.url.path,
        )
    if moment < now - HORIZON_MS:
        return problem_response(
            422,
            "TIME_BEFORE_HORIZON",
            "That moment is too far back",
            detail=f"at={at} is more than 24 hours ago. Time travel reaches back 24 hours; "
            "every event is still stored.",
            instance=request.url.path,
        )
    asked_for_now = moment >= now  # "now", give or take the clock skew allowed above
    moment = min(moment, now)
    slots: Slots = request.app.state.rebuild_slots
    if not slots.try_acquire(admin=is_admin(request.headers.get("x-role"))):
        return problem_response(
            503,
            "OVERLOADED",
            "Time travel is busy",
            detail="Too many time-travel requests at once on this replica; please retry shortly.",
            instance=request.url.path,
        )
    try:
        # A moment at or after the live market's last event is "now": prove the rebuild exact.
        live = projection.markets[ticker].as_of_ms
        verify = asked_for_now or live is None or moment >= live
        market = await time_travel.market_at(ticker, moment, verify=verify, now=asked_for_now)
    except httpx.HTTPError as exc:
        logger.warning("event store unavailable", extra={"error": repr(exc)})
        return problem_response(
            503,
            "DEPENDENCY_UNAVAILABLE",
            "Temporarily unavailable",
            detail="matching-engine's event store is not answering; please retry shortly.",
            instance=request.url.path,
        )
    finally:
        slots.release()
    return JSONResponse(time_travel_view(market, depth))
