"""Stub routes for every operation in api/market-data-service.yaml.

Generated once by tools/gen_stubs.py (contract first). From now on edit this file by hand:
replace a stub with the real route when the operation is built. tests/contract/ fails if this
file and the spec disagree.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from market_data_service.runtime.problem import not_implemented

router = APIRouter()


@router.get(
    "/v1/instruments",
    operation_id="listInstruments",
    summary="List instruments with last price and change",
    openapi_extra={"x-seeds-id": "Q-02"},
)
async def list_instruments(request: Request) -> JSONResponse:
    return not_implemented(request, "Q-02")


@router.get(
    "/v1/market/{ticker}/candles",
    operation_id="getCandles",
    summary="Get candles for a ticker",
    openapi_extra={"x-seeds-id": "Q-03"},
)
async def get_candles(request: Request) -> JSONResponse:
    return not_implemented(request, "Q-03")


@router.get(
    "/v1/market/{ticker}/order-book",
    operation_id="getOrderBook",
    summary="Get order-book depth for a ticker",
    openapi_extra={"x-seeds-id": "Q-04"},
)
async def get_order_book(request: Request) -> JSONResponse:
    return not_implemented(request, "Q-04")


@router.get(
    "/v1/market/{ticker}/trades",
    operation_id="getRecentTrades",
    summary="Get recent trades for a ticker",
    openapi_extra={"x-seeds-id": "Q-05"},
)
async def get_recent_trades(request: Request) -> JSONResponse:
    return not_implemented(request, "Q-05")


@router.get(
    "/v1/time-travel/{ticker}",
    operation_id="getMarketAt",
    summary="Get the market as it was at time T",
    openapi_extra={"x-seeds-id": "Q-12"},
)
async def get_market_at(request: Request) -> JSONResponse:
    return not_implemented(request, "Q-12")


@router.post(
    "/internal/v1/faults",
    operation_id="injectFault",
    summary="Inject a fault (chaos hook)",
    openapi_extra={"x-seeds-id": "A-12"},
)
async def inject_fault(request: Request) -> JSONResponse:
    return not_implemented(request, "A-12")


@router.delete(
    "/internal/v1/faults",
    operation_id="clearFault",
    summary="Clear the active fault (chaos hook)",
    openapi_extra={"x-seeds-id": "A-13"},
)
async def clear_fault(request: Request) -> JSONResponse:
    return not_implemented(request, "A-13")
