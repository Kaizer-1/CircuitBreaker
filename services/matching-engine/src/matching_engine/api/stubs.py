"""Stub routes for every operation in api/matching-engine.yaml.

Generated once by tools/gen_stubs.py (contract first). From now on edit this file by hand:
replace a stub with the real route when the operation is built. tests/contract/ fails if this
file and the spec disagree.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from matching_engine.runtime.problem import not_implemented

router = APIRouter()


@router.get(
    "/internal/v1/events",
    operation_id="listMarketEvents",
    summary="Read market events by sequence",
    openapi_extra={"x-seeds-id": "Q-14"},
)
async def list_market_events(request: Request) -> JSONResponse:
    return not_implemented(request, "Q-14")


@router.get(
    "/internal/v1/snapshots/{ticker}",
    operation_id="getBookSnapshot",
    summary="Get the nearest order-book snapshot at or before a time",
    openapi_extra={"x-seeds-id": "Q-15"},
)
async def get_book_snapshot(request: Request) -> JSONResponse:
    return not_implemented(request, "Q-15")


@router.get(
    "/internal/v1/books/{ticker}",
    operation_id="getLiveBook",
    summary="Get the engine's live in-memory order book",
    openapi_extra={"x-seeds-id": "Q-16"},
)
async def get_live_book(request: Request) -> JSONResponse:
    return not_implemented(request, "Q-16")
