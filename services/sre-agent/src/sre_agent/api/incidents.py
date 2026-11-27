"""Q-17, Q-18, A-15, Q-20, A-14 (api/sre-agent.yaml) and the live feed Q-19.

api-gateway checks the role first (Q-17/Q-18/Q-19 any signed-in role; A-15, Q-20 and A-14 admin
only) and sets X-Role and X-Nickname; the agent checks the admin role again. There is deliberately
no route to change the model or the tools.
"""

import asyncio
import contextlib
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from sre_agent.application.agent import Agent
from sre_agent.domain.incident import Incident, NotPendingError, Status, iso, parse_time
from sre_agent.ports.store import AgentSettings, Store
from sre_agent.runtime.problem import problem_response

router = APIRouter()
Role = Annotated[str | None, Header(alias="X-Role")]
Nickname = Annotated[str | None, Header(alias="X-Nickname")]
RESOLVED_IN_SNAPSHOT = 20


class IncidentStatus(StrEnum):
    OPEN = "OPEN"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    RESOLVED = "RESOLVED"


class Decision(StrEnum):
    APPROVE = "APPROVE"
    DECLINE = "DECLINE"


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    remediation_id: str = Field(min_length=36, max_length=36)
    decision: Decision
    comment: str | None = Field(default=None, max_length=280)


class SettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approval_required: bool | None = None
    paused: bool | None = None


def _started(request: Request | WebSocket) -> tuple[Agent, Store] | None:
    agent: Agent | None = request.app.state.agent
    store: Store | None = request.app.state.store
    return None if agent is None or store is None else (agent, store)


def _starting(request: Request) -> JSONResponse:
    return problem_response(
        503,
        "DEPENDENCY_UNAVAILABLE",
        "Temporarily unavailable",
        detail="The SRE agent is starting: its incident store is not ready yet.",
        instance=request.url.path,
    )


def _forbidden(request: Request) -> JSONResponse:
    return problem_response(
        403,
        "FORBIDDEN",
        "Forbidden",
        detail="Approving fixes and changing the agent's settings is for the admin role only.",
        instance=request.url.path,
    )


def _freshest(agent: Agent, incident: Incident) -> Incident:
    return agent.incident(incident.incident_id) or incident


def settings_view(settings: AgentSettings, agent: Agent) -> dict[str, Any]:
    return {
        "environment": settings.environment,
        "approval_required": settings.approval_required,
        "paused": settings.paused,
        "model": agent.model,
        "updated_at": iso(settings.updated_at),
        "updated_by": settings.updated_by,
        "budget": agent.budget(),
    }


@router.get(
    "/v1/incidents",
    operation_id="listIncidents",
    summary="List incidents",
    openapi_extra={"x-seeds-id": "Q-17"},
)
async def list_incidents(
    request: Request,
    status: IncidentStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> JSONResponse:
    before: datetime | None = None
    if cursor:
        try:
            before = parse_time(cursor)
        except ValueError:
            return problem_response(
                400, "BAD_REQUEST", "Bad request", detail="Invalid cursor.", instance="cursor"
            )
    started = _started(request)
    if started is None:
        return _starting(request)
    agent, store = started
    items: list[Incident] = []
    page_before = before
    for _ in range(10):  # a status filter may need several pages
        page = await store.recent(max(limit, 50), page_before)
        if not page:
            break
        for incident in page:
            fresh = _freshest(agent, incident)
            if status is None or fresh.status.value == status.value:
                items.append(fresh)
        page_before = page[-1].opened_at
        if len(items) >= limit:
            break
    items = items[:limit]
    next_cursor = iso(items[-1].opened_at) if len(items) == limit else None
    return JSONResponse({"items": [i.to_summary() for i in items], "next_cursor": next_cursor})


@router.get(
    "/v1/incidents/{incident_id}",
    operation_id="getIncident",
    summary="Get one incident with its timeline",
    openapi_extra={"x-seeds-id": "Q-18"},
)
async def get_incident(request: Request, incident_id: str) -> JSONResponse:
    started = _started(request)
    if started is None:
        return _starting(request)
    agent, store = started
    incident = agent.incident(incident_id) or await store.get(incident_id)
    if incident is None:
        return problem_response(
            404, "NOT_FOUND", "Not found", detail="No such incident.", instance=request.url.path
        )
    return JSONResponse(incident.to_api())


@router.post(
    "/v1/incidents/{incident_id}/decision",
    operation_id="decideRemediation",
    summary="Approve or decline a proposed remediation",
    openapi_extra={"x-seeds-id": "A-15"},
)
async def decide_remediation(
    request: Request,
    incident_id: str,
    body: DecisionBody,
    role: Role = None,
    nickname: Nickname = None,
) -> JSONResponse:
    if role != "admin":
        return _forbidden(request)
    started = _started(request)
    if started is None:
        return _starting(request)
    agent, store = started
    if agent.incident(incident_id) is None and await store.get(incident_id) is None:
        return problem_response(
            404, "NOT_FOUND", "Not found", detail="No such incident.", instance=request.url.path
        )
    try:
        remediation = await agent.decide(
            incident_id,
            body.remediation_id,
            body.decision is Decision.APPROVE,
            nickname or "admin",
            body.comment,
        )
    except NotPendingError as exc:
        return problem_response(
            409,
            "NO_PENDING_REMEDIATION",
            "Nothing to decide",
            detail=str(exc),
            instance=request.url.path,
        )
    return JSONResponse(remediation.to_api())


@router.get(
    "/v1/agent/settings",
    operation_id="getAgentSettings",
    summary="Get agent settings",
    openapi_extra={"x-seeds-id": "Q-20"},
)
async def get_agent_settings(request: Request, role: Role = None) -> JSONResponse:
    if role != "admin":
        return _forbidden(request)
    started = _started(request)
    if started is None:
        return _starting(request)
    agent, _ = started
    return JSONResponse(settings_view(agent.settings, agent))


@router.put(
    "/v1/agent/settings",
    operation_id="updateAgentSettings",
    summary="Update agent settings",
    openapi_extra={"x-seeds-id": "A-14"},
)
async def update_agent_settings(
    request: Request, body: SettingsBody, role: Role = None, nickname: Nickname = None
) -> JSONResponse:
    if role != "admin":
        return _forbidden(request)
    if body.approval_required is None and body.paused is None:
        return problem_response(
            422,
            "VALIDATION_FAILED",
            "Validation failed",
            detail="Give approval_required, paused or both.",
            instance=request.url.path,
        )
    started = _started(request)
    if started is None:
        return _starting(request)
    agent, _ = started
    settings = await agent.update_settings(body.approval_required, body.paused, nickname or "admin")
    return JSONResponse(settings_view(settings, agent))


@router.websocket("/v1/ws/incidents")
async def incident_feed(websocket: WebSocket) -> None:
    """Q-19: a snapshot (open incidents and the last 20 resolved), then every change."""
    started = _started(websocket)
    await websocket.accept()
    if started is None:
        await websocket.close(1011, "the agent is starting")
        return
    agent, store = started
    queue = agent.feed.subscribe()
    try:
        recent = await store.recent(50)
        open_ids = {i.incident_id for i in agent.open_incidents()}
        resolved = [
            i for i in recent if i.status is Status.RESOLVED and i.incident_id not in open_ids
        ]
        snapshot = [i.to_summary() for i in agent.open_incidents()] + [
            i.to_summary() for i in resolved[:RESOLVED_IN_SNAPSHOT]
        ]
        await websocket.send_json({"type": "snapshot", "data": snapshot})
        vitals = getattr(websocket.app.state, "vitals", None)
        current = vitals.current() if vitals is not None else None
        if current is not None:  # a fresh screen need not wait for the next poll
            await websocket.send_json(current)
        receiver = asyncio.create_task(_drain(websocket))
        try:
            while not receiver.done():
                getter = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait(
                    {getter, receiver}, return_when=asyncio.FIRST_COMPLETED
                )
                if getter in done:
                    await websocket.send_json(getter.result())
                else:
                    getter.cancel()
        finally:
            receiver.cancel()
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect):
                await receiver
    except WebSocketDisconnect:
        pass
    finally:
        agent.feed.unsubscribe(queue)


async def _drain(websocket: WebSocket) -> None:
    """The client sends nothing; this only notices it leaving."""
    while True:
        await websocket.receive_text()
