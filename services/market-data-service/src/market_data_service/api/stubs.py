"""Stub routes for every operation in api/market-data-service.yaml.

Generated once by tools/gen_stubs.py (contract first). From now on edit this file by hand:
replace a stub with the real route when the operation is built. tests/contract/ fails if this
file and the spec disagree. Q-02 to Q-05 are built (api/market.py), Q-12 too (api/time_travel.py);
A-12 and A-13 are Phase 7.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from market_data_service.runtime.problem import not_implemented

router = APIRouter()


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
