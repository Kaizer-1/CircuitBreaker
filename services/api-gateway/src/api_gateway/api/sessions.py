"""The operations the gateway implements itself: A-01, A-03, A-04 and Q-01 (sequence diagram 1)."""

import hmac
import logging
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from api_gateway.api.auth import ANY_ROLE, ApiProblemError, limit_by_ip
from api_gateway.domain.nicknames import NicknameBlocklist
from api_gateway.domain.sessions import Principal, Role, Sessions
from api_gateway.runtime.events import format_timestamp
from api_gateway.runtime.jsonlog import correlation_id_var
from api_gateway.runtime.problem import FieldError, problem_response

logger = logging.getLogger("sessions")

router = APIRouter()
Nickname = Annotated[str, Field(pattern=r"^[A-Za-z0-9_]{3,20}$")]


class JoinRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nickname: Nickname


class BotSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_name: Annotated[str, Field(pattern=r"^(bot|load)-[0-9]{2}$")]


class AdminSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nickname: Nickname
    passcode: Annotated[str, Field(min_length=8)]


def _session(
    request: Request, sub: str, role: Role, nickname: str, trader_id: str | None
) -> JSONResponse:
    sessions: Sessions = request.app.state.sessions
    token, expires = sessions.issue(sub, role, nickname)
    body = {
        "access_token": token,
        "token_type": "Bearer",
        "expires_at": format_timestamp(expires),
        "trader_id": trader_id,
        "nickname": nickname,
        "role": role.value,
    }
    return JSONResponse(body, status_code=201)


async def _open_account(request: Request, nickname: str, kind: str) -> dict[str, Any]:
    """A-02 on portfolio-service. 409 NICKNAME_TAKEN passes through; anything else is 503."""
    base = request.app.state.settings.service_upstreams.get("portfolio-service")
    client: httpx.AsyncClient = request.app.state.http_client
    if base is None:
        raise ApiProblemError(503, "DEPENDENCY_UNAVAILABLE", "Unavailable", "No portfolio-service.")
    try:
        response = await client.post(
            f"{base}/internal/v1/accounts",
            json={"nickname": nickname, "kind": kind},
            headers={"X-Correlation-Id": correlation_id_var.get() or ""},
        )
    except httpx.HTTPError as exc:
        logger.warning("portfolio-service unavailable", extra={"error": type(exc).__name__})
        raise ApiProblemError(
            503,
            "DEPENDENCY_UNAVAILABLE",
            "Temporarily unavailable",
            "portfolio-service did not answer.",
        ) from exc
    if response.status_code == 409:
        raise ApiProblemError(
            409,
            "NICKNAME_TAKEN",
            "Nickname taken",
            f"Someone is already trading as {nickname}. Please pick another nickname.",
        )
    if response.status_code not in (200, 201):
        raise ApiProblemError(
            503,
            "DEPENDENCY_UNAVAILABLE",
            "Temporarily unavailable",
            f"portfolio-service answered {response.status_code}.",
        )
    account: dict[str, Any] = response.json()
    return account


@router.post(
    "/v1/sessions",
    operation_id="joinMarket",
    summary="Join the market as a trader",
    openapi_extra={"x-seeds-id": "A-01"},
    dependencies=[Depends(limit_by_ip)],
)
async def join_market(request: Request, body: JoinRequest) -> JSONResponse:
    blocklist: NicknameBlocklist = request.app.state.nickname_blocklist
    if blocklist.refuses(body.nickname):
        # Projector safety: refused before any account exists. The name itself is not logged.
        logger.info("nickname refused by the blocklist")
        return problem_response(
            422,
            "VALIDATION_FAILED",
            "Please choose another nickname",
            detail="That nickname can't be shown on the big screen. Please choose another one.",
            instance=request.url.path,
            errors=[FieldError(field="nickname", message="Please choose another nickname.")],
        )
    account = await _open_account(request, body.nickname, "HUMAN")
    return _session(request, account["trader_id"], Role.TRADER, body.nickname, account["trader_id"])


@router.post(
    "/v1/bot-sessions",
    operation_id="startBotSession",
    summary="Start a bot session",
    openapi_extra={"x-seeds-id": "A-03"},
    dependencies=[Depends(limit_by_ip)],
)
async def start_bot_session(
    request: Request,
    body: BotSessionRequest,
    bot_secret: Annotated[str, Header(alias="X-Bot-Secret")],
) -> JSONResponse:
    expected = request.app.state.settings.bot_secret.get_secret_value()
    if not hmac.compare_digest(bot_secret.encode(), expected.encode()):
        raise ApiProblemError(401, "UNAUTHENTICATED", "Not signed in", "The bot secret is wrong.")
    kind = "BOT" if body.account_name.startswith("bot-") else "LOAD"
    account = await _open_account(request, body.account_name, kind)
    return _session(
        request, account["trader_id"], Role.BOT, body.account_name, account["trader_id"]
    )


@router.post(
    "/v1/admin/sessions",
    operation_id="startAdminSession",
    summary="Start an admin session",
    openapi_extra={"x-seeds-id": "A-04"},
    dependencies=[Depends(limit_by_ip)],
)
async def start_admin_session(request: Request, body: AdminSessionRequest) -> JSONResponse:
    expected = request.app.state.settings.admin_passcode.get_secret_value()
    if not hmac.compare_digest(body.passcode.encode(), expected.encode()):
        raise ApiProblemError(
            401, "INVALID_PASSCODE", "Wrong passcode", "The admin passcode is incorrect."
        )
    return _session(request, body.nickname, Role.ADMIN, body.nickname, None)


@router.get(
    "/v1/session",
    operation_id="getCurrentSession",
    summary="Get my current session",
    openapi_extra={"x-seeds-id": "Q-01"},
)
async def get_current_session(principal: Annotated[Principal, Depends(ANY_ROLE)]) -> dict[str, Any]:
    return {
        "trader_id": principal.trader_id,
        "nickname": principal.nickname,
        "role": principal.role.value,
        "expires_at": format_timestamp(principal.expires_at),
    }
