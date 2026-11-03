"""Stub routes for every operation in api/portfolio-service.yaml.

Generated once by tools/gen_stubs.py (contract first). From now on edit this file by hand:
replace a stub with the real route when the operation is built. tests/contract/ fails if this
file and the spec disagree.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from portfolio_service.runtime.problem import not_implemented

router = APIRouter()


@router.get(
    "/v1/portfolio",
    operation_id="getMyPortfolio",
    summary="Get my portfolio",
    openapi_extra={"x-seeds-id": "Q-09"},
)
async def get_my_portfolio(request: Request) -> JSONResponse:
    return not_implemented(request, "Q-09")


@router.get(
    "/v1/leaderboard",
    operation_id="getLeaderboard",
    summary="Get the leaderboard",
    openapi_extra={"x-seeds-id": "Q-11"},
)
async def get_leaderboard(request: Request) -> JSONResponse:
    return not_implemented(request, "Q-11")


@router.post(
    "/internal/v1/accounts",
    operation_id="openAccount",
    summary="Open a trading account",
    openapi_extra={"x-seeds-id": "A-02"},
)
async def open_account(request: Request) -> JSONResponse:
    return not_implemented(request, "A-02")


@router.post(
    "/internal/v1/reservations",
    operation_id="createReservation",
    summary="Reserve cash or shares for an order",
    openapi_extra={"x-seeds-id": "A-06"},
)
async def create_reservation(request: Request) -> JSONResponse:
    return not_implemented(request, "A-06")
