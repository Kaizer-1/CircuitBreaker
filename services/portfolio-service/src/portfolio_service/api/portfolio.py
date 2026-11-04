"""Q-09 and the internal operations A-02 and A-06 (api/portfolio-service.yaml), and the Q-10
WebSocket (api/README.md). Thin: parse, call a use case, map its errors to Problem Details."""

import asyncio
import contextlib
import json
import time
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from portfolio_service.application.live import PortfolioHub, Subscriber
from portfolio_service.application.portfolio import PortfolioService
from portfolio_service.domain.model import (
    Account,
    AccountKind,
    AccountNotFoundError,
    InsufficientFundsError,
    InsufficientSharesError,
    InvalidReservationError,
    NicknameTakenError,
    OrderType,
    Reservation,
    ReservationRequest,
    Side,
)
from portfolio_service.runtime.problem import FieldError, problem_response

Ticker = Literal["CHAIWALA", "JUGAAD", "DABBA", "MONSOON", "FILMY"]

router = APIRouter()


class OpenAccountBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nickname: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{3,20}$")]
    kind: AccountKind


class ReservationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: UUID
    trader_id: UUID
    ticker: Ticker
    side: Side
    order_type: OrderType
    quantity: Annotated[int, Field(ge=1, le=10_000)]
    limit_price_paise: Annotated[int | None, Field(multiple_of=5, ge=5)]


def _service(request: Request) -> PortfolioService | None:
    service: PortfolioService | None = getattr(request.app.state, "service", None)
    return service


def _starting(request: Request) -> JSONResponse:
    return problem_response(
        503,
        "DEPENDENCY_UNAVAILABLE",
        "Starting up",
        detail="portfolio-service is connecting to its database.",
        instance=request.url.path,
    )


def _account_body(a: Account) -> dict[str, Any]:
    return {
        "trader_id": a.trader_id,
        "nickname": a.nickname,
        "kind": a.kind.value,
        "cash_paise": a.cash_paise,
        "created_at": a.created_at,
    }


def _reservation_body(r: Reservation) -> dict[str, Any]:
    return {
        "reservation_id": r.reservation_id,
        "order_id": r.order_id,
        "status": r.status.value,
        "reserved_cash_paise": r.reserved_cash_paise,
        "reserved_quantity": r.reserved_quantity,
        "protection_price_paise": r.protection_price_paise,
        "created_at": r.created_at,
    }


@router.get(
    "/v1/portfolio",
    operation_id="getMyPortfolio",
    summary="Get my portfolio",
    openapi_extra={"x-seeds-id": "Q-09"},
)
async def get_my_portfolio(request: Request, x_trader_id: Annotated[UUID, Header()]) -> Any:
    service = _service(request)
    if service is None:
        return _starting(request)
    try:
        return await service.portfolio(str(x_trader_id))
    except AccountNotFoundError:
        return problem_response(
            404, "NOT_FOUND", "Not found", detail="No such account.", instance=request.url.path
        )


@router.post(
    "/internal/v1/accounts",
    operation_id="openAccount",
    summary="Open a trading account",
    openapi_extra={"x-seeds-id": "A-02"},
)
async def open_account(request: Request, body: OpenAccountBody) -> Any:
    service = _service(request)
    if service is None:
        return _starting(request)
    try:
        account, created = await service.open_account(body.nickname, body.kind)
    except NicknameTakenError:
        return problem_response(
            409,
            "NICKNAME_TAKEN",
            "Nickname taken",
            detail=f"Someone is already trading as {body.nickname}. Please pick another nickname.",
            instance=request.url.path,
        )
    return JSONResponse(_account_body(account), status_code=201 if created else 200)


@router.post(
    "/internal/v1/reservations",
    operation_id="createReservation",
    summary="Reserve cash or shares for an order",
    openapi_extra={"x-seeds-id": "A-06"},
)
async def create_reservation(request: Request, body: ReservationBody) -> Any:
    service = _service(request)
    if service is None:
        return _starting(request)
    metrics = request.app.state.business_metrics
    order = ReservationRequest(
        order_id=str(body.order_id),
        trader_id=str(body.trader_id),
        ticker=body.ticker,
        side=body.side,
        order_type=body.order_type,
        quantity=body.quantity,
        limit_price_paise=body.limit_price_paise,
    )
    try:
        reservation, created = await service.create_reservation(order)
    except AccountNotFoundError:
        return problem_response(
            404, "NOT_FOUND", "Not found", detail="No such account.", instance=request.url.path
        )
    except InsufficientFundsError as exc:
        metrics.reservations_total.labels(body.side, "refused").inc()
        return problem_response(
            422,
            "INSUFFICIENT_FUNDS",
            "Not enough cash",
            detail=f"Needs {exc.needed} paise, available {exc.available} paise.",
            instance=request.url.path,
        )
    except InsufficientSharesError as exc:
        metrics.reservations_total.labels(body.side, "refused").inc()
        return problem_response(
            422,
            "INSUFFICIENT_SHARES",
            "Not enough shares",
            detail=f"You have {exc.available} available {exc.ticker} shares; "
            f"this order sells {exc.needed}.",
            instance=request.url.path,
        )
    except InvalidReservationError as exc:
        return problem_response(
            422,
            "VALIDATION_FAILED",
            "Validation failed",
            detail=str(exc),
            instance=request.url.path,
            errors=[FieldError(field="limit_price_paise", message=str(exc))],
        )
    metrics.reservations_total.labels(body.side, "created" if created else "repeat").inc()
    return JSONResponse(_reservation_body(reservation), status_code=201 if created else 200)


# ---------------------------------------------------------------------- Q-10 WebSocket


@router.websocket("/v1/ws/portfolio")
async def stream_portfolio(websocket: WebSocket) -> None:
    """The caller's own portfolio, live. Whose it is comes only from `X-Trader-Id`, which the
    gateway sets from the verified token (and strips from clients), so nobody can watch another
    trader's stream."""
    await websocket.accept()
    hub: PortfolioHub | None = getattr(websocket.app.state, "hub", None)
    try:
        trader_id = str(UUID(websocket.headers.get("x-trader-id", "")))
    except ValueError:
        await websocket.close(code=4401, reason="no trader identity")
        return
    if hub is None:
        await websocket.close(code=1011, reason="portfolio-service is starting")
        return
    subscriber = hub.subscribe(trader_id)
    settings = websocket.app.state.settings
    try:
        try:
            first = await hub.view(subscriber)
        except AccountNotFoundError:
            await websocket.close(code=4404, reason="no such account")
            return
        await websocket.send_json(first)
        await serve(
            websocket, subscriber, settings.ws_ping_seconds, settings.ws_pong_timeout_seconds
        )
    except WebSocketDisconnect:
        pass
    finally:
        hub.unsubscribe(subscriber)


async def serve(
    websocket: WebSocket, subscriber: Subscriber, ping_every: float, pong_timeout: float
) -> None:
    """Send queued messages, answer the heartbeat (api/README.md: ping every 15 s, closed after
    45 s without a pong), until either side closes."""
    last_pong = time.monotonic()

    async def send() -> None:
        while True:
            message = await subscriber.queue.get()
            if subscriber.lagging:
                await websocket.close(code=1011, reason="too far behind: reconnect")
                return
            await websocket.send_json(message)

    async def receive() -> None:
        nonlocal last_pong
        while True:
            try:
                message: Any = json.loads(await websocket.receive_text())
            except ValueError:
                continue
            if isinstance(message, dict) and message.get("type") == "pong":
                last_pong = time.monotonic()

    async def ping() -> None:
        while True:
            await asyncio.sleep(ping_every)
            if time.monotonic() - last_pong > pong_timeout:
                await websocket.close(code=1000, reason="no pong")
                return
            await websocket.send_json({"type": "ping"})

    tasks = [asyncio.create_task(t()) for t in (send, receive, ping)]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
