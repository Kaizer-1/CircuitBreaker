"""Stub routes for every operation in api/sre-agent.yaml.

Generated once by tools/gen_stubs.py (contract first). From now on edit this file by hand:
replace a stub with the real route when the operation is built. tests/contract/ fails if this
file and the spec disagree.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from sre_agent.runtime.problem import not_implemented

router = APIRouter()


@router.get(
    "/v1/incidents",
    operation_id="listIncidents",
    summary="List incidents",
    openapi_extra={"x-seeds-id": "Q-17"},
)
async def list_incidents(request: Request) -> JSONResponse:
    return not_implemented(request, "Q-17")


@router.get(
    "/v1/incidents/{incident_id}",
    operation_id="getIncident",
    summary="Get one incident with its timeline",
    openapi_extra={"x-seeds-id": "Q-18"},
)
async def get_incident(request: Request) -> JSONResponse:
    return not_implemented(request, "Q-18")


@router.post(
    "/v1/incidents/{incident_id}/decision",
    operation_id="decideRemediation",
    summary="Approve or decline a proposed remediation",
    openapi_extra={"x-seeds-id": "A-15"},
)
async def decide_remediation(request: Request) -> JSONResponse:
    return not_implemented(request, "A-15")


@router.get(
    "/v1/agent/settings",
    operation_id="getAgentSettings",
    summary="Get agent settings",
    openapi_extra={"x-seeds-id": "Q-20"},
)
async def get_agent_settings(request: Request) -> JSONResponse:
    return not_implemented(request, "Q-20")


@router.put(
    "/v1/agent/settings",
    operation_id="updateAgentSettings",
    summary="Update agent settings",
    openapi_extra={"x-seeds-id": "A-14"},
)
async def update_agent_settings(request: Request) -> JSONResponse:
    return not_implemented(request, "A-14")
