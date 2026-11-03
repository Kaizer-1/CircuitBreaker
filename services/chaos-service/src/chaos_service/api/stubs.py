"""Stub routes for every operation in api/chaos-service.yaml.

Generated once by tools/gen_stubs.py (contract first). From now on edit this file by hand:
replace a stub with the real route when the operation is built. tests/contract/ fails if this
file and the spec disagree.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from chaos_service.runtime.problem import not_implemented

router = APIRouter()


@router.get(
    "/v1/chaos/scenarios",
    operation_id="listChaosScenarios",
    summary="List chaos scenarios",
    openapi_extra={"x-seeds-id": "Q-21"},
)
async def list_chaos_scenarios(request: Request) -> JSONResponse:
    return not_implemented(request, "Q-21")


@router.get(
    "/v1/chaos/experiments",
    operation_id="listChaosExperiments",
    summary="List chaos experiments",
    openapi_extra={"x-seeds-id": "Q-22"},
)
async def list_chaos_experiments(request: Request) -> JSONResponse:
    return not_implemented(request, "Q-22")


@router.post(
    "/v1/chaos/experiments",
    operation_id="startChaosExperiment",
    summary="Start a chaos experiment",
    openapi_extra={"x-seeds-id": "A-10"},
)
async def start_chaos_experiment(request: Request) -> JSONResponse:
    return not_implemented(request, "A-10")


@router.get(
    "/v1/chaos/experiments/{experiment_id}",
    operation_id="getChaosExperiment",
    summary="Get one chaos experiment",
    openapi_extra={"x-seeds-id": "Q-23"},
)
async def get_chaos_experiment(request: Request) -> JSONResponse:
    return not_implemented(request, "Q-23")


@router.post(
    "/v1/chaos/experiments/{experiment_id}/stop",
    operation_id="stopChaosExperiment",
    summary="Stop a chaos experiment",
    openapi_extra={"x-seeds-id": "A-11"},
)
async def stop_chaos_experiment(request: Request) -> JSONResponse:
    return not_implemented(request, "A-11")
