"""Stub routes for the operations in api/portfolio-service.yaml that are not built yet.

Generated once by tools/gen_stubs.py (contract first). From now on edit this file by hand:
replace a stub with the real route when the operation is built. tests/contract/ fails if this
file and the spec disagree. Q-11 (leaderboard) is built in Phase 4.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from portfolio_service.runtime.problem import not_implemented

router = APIRouter()


@router.get(
    "/v1/leaderboard",
    operation_id="getLeaderboard",
    summary="Get the leaderboard",
    openapi_extra={"x-seeds-id": "Q-11"},
)
async def get_leaderboard(request: Request) -> JSONResponse:
    return not_implemented(request, "Q-11")
