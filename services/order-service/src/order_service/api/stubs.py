"""Stub routes for every operation in api/order-service.yaml.

Generated once by tools/gen_stubs.py (contract first). From now on edit this file by hand:
replace a stub with the real route when the operation is built. tests/contract/ fails if this
file and the spec disagree.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from order_service.runtime.problem import not_implemented

router = APIRouter()


@router.get(
    "/v1/orders",
    operation_id="listMyOrders",
    summary="List my orders",
    openapi_extra={"x-seeds-id": "Q-07"},
)
async def list_my_orders(request: Request) -> JSONResponse:
    return not_implemented(request, "Q-07")


@router.post(
    "/v1/orders",
    operation_id="placeOrder",
    summary="Place an order",
    openapi_extra={"x-seeds-id": "A-05"},
)
async def place_order(request: Request) -> JSONResponse:
    return not_implemented(request, "A-05")


@router.get(
    "/v1/orders/{order_id}",
    operation_id="getOrder",
    summary="Get one of my orders",
    openapi_extra={"x-seeds-id": "Q-08"},
)
async def get_order(request: Request) -> JSONResponse:
    return not_implemented(request, "Q-08")


@router.delete(
    "/v1/orders/{order_id}",
    operation_id="cancelOrder",
    summary="Cancel an order",
    openapi_extra={"x-seeds-id": "A-07"},
)
async def cancel_order(request: Request) -> JSONResponse:
    return not_implemented(request, "A-07")
