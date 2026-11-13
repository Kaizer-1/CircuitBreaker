"""A-05, A-07, Q-07, Q-08 and the internal A-19 (api/order-service.yaml). Thin routers: parse, call
a use case, map its errors to Problem Details."""

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from order_service.application.orders import (
    NotCancellableError,
    OrderNotFoundError,
    Orders,
    RejectedOrderError,
)
from order_service.domain.order import (
    Order,
    OrderRequest,
    OrderType,
    OrderValidationError,
    RejectionReason,
    Side,
)
from order_service.ports.portfolio import DependencyUnavailableError
from order_service.runtime.jsonlog import correlation_id_var
from order_service.runtime.problem import FieldError, problem_response

Ticker = Literal["CALVERA", "KORVIN", "ASTRYX", "HELIOR", "TAVIRA"]
StatusFilter = Literal[
    "open", "PENDING", "OPEN", "PARTIALLY_FILLED", "FILLED", "CANCELLED", "REJECTED"
]
TraderId = Annotated[UUID, Header(alias="X-Trader-Id")]

TITLES = {
    RejectionReason.UNKNOWN_TICKER: "Unknown ticker",
    RejectionReason.INVALID_TICK_SIZE: "Invalid price",
    RejectionReason.VALIDATION_FAILED: "Validation failed",
    RejectionReason.INSUFFICIENT_FUNDS: "Not enough cash",
    RejectionReason.INSUFFICIENT_SHARES: "Not enough shares",
}

router = APIRouter()


class PlaceOrderBody(BaseModel):
    """Parsed loosely (ticker as text) so the rules can answer with their specific codes."""

    model_config = ConfigDict(extra="forbid")
    ticker: str
    side: Side
    order_type: OrderType
    quantity: int
    limit_price_paise: int | None = None


class ExpiryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: UUID
    trader_id: UUID
    ticker: Ticker


def order_body(o: Order) -> dict[str, Any]:
    return {
        "order_id": o.order_id,
        "trader_id": o.trader_id,
        "ticker": o.ticker,
        "side": o.side.value,
        "order_type": o.order_type.value,
        "quantity": o.quantity,
        "limit_price_paise": o.limit_price_paise,
        "protection_price_paise": o.protection_price_paise,
        "status": o.status.value,
        "filled_quantity": o.filled_quantity,
        "remaining_quantity": o.remaining_quantity,
        "average_fill_price_paise": o.average_fill_price_paise,
        "cancel_requested": o.cancel_requested,
        "cancel_reason": o.cancel_reason,
        "rejection_reason": o.rejection_reason.value if o.rejection_reason else None,
        "placed_at": o.placed_at,
        "updated_at": o.updated_at,
    }


def _orders(request: Request) -> Orders | None:
    orders: Orders | None = getattr(request.app.state, "orders", None)
    return orders


def _unavailable(request: Request, detail: str) -> JSONResponse:
    return problem_response(
        503,
        "DEPENDENCY_UNAVAILABLE",
        "Temporarily unavailable",
        detail=detail,
        instance=request.url.path,
    )


def _not_found(request: Request) -> JSONResponse:
    return problem_response(
        404, "NOT_FOUND", "Not found", detail="No such order.", instance=request.url.path
    )


@router.post(
    "/v1/orders",
    operation_id="placeOrder",
    summary="Place an order",
    openapi_extra={"x-seeds-id": "A-05"},
)
async def place_order(
    request: Request,
    body: PlaceOrderBody,
    trader_id: TraderId,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=64)] = None,
) -> Any:
    orders = _orders(request)
    if orders is None:
        return _unavailable(request, "order-service is starting.")
    order_request = OrderRequest(
        body.ticker, body.side, body.order_type, body.quantity, body.limit_price_paise
    )
    try:
        order = await orders.place(
            str(trader_id),
            order_request,
            correlation_id=correlation_id_var.get() or "none",
            idempotency_key=idempotency_key,
        )
    except OrderValidationError as exc:
        return problem_response(
            422,
            exc.code.value,
            TITLES[exc.code],
            detail=exc.message,
            instance=request.url.path,
            errors=[FieldError(field=exc.field, message=exc.message)],
        )
    except RejectedOrderError as exc:
        reason = exc.order.rejection_reason or RejectionReason.VALIDATION_FAILED
        return problem_response(
            422,
            reason.value,
            TITLES.get(reason, "Rejected"),
            detail=exc.detail,
            instance=request.url.path,
        )
    except DependencyUnavailableError as exc:
        return _unavailable(request, str(exc))
    return JSONResponse(
        order_body(order), status_code=202, headers={"Location": f"/v1/orders/{order.order_id}"}
    )


@router.get(
    "/v1/orders",
    operation_id="listMyOrders",
    summary="List my orders",
    openapi_extra={"x-seeds-id": "Q-07"},
)
async def list_my_orders(
    request: Request,
    trader_id: TraderId,
    status: StatusFilter | None = None,
    ticker: Ticker | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> Any:
    orders = _orders(request)
    if orders is None:
        return _unavailable(request, "order-service is starting.")
    try:
        items, next_cursor = await orders.list(
            str(trader_id), status=status, ticker=ticker, limit=limit, cursor=cursor
        )
    except ValueError:
        return problem_response(
            400, "BAD_REQUEST", "Bad request", detail="Invalid cursor.", instance=request.url.path
        )
    return {"items": [order_body(o) for o in items], "next_cursor": next_cursor}


@router.get(
    "/v1/orders/{order_id}",
    operation_id="getOrder",
    summary="Get one of my orders",
    openapi_extra={"x-seeds-id": "Q-08"},
)
async def get_order(request: Request, order_id: UUID, trader_id: TraderId) -> Any:
    orders = _orders(request)
    if orders is None:
        return _unavailable(request, "order-service is starting.")
    try:
        return order_body(await orders.get(str(trader_id), str(order_id)))
    except OrderNotFoundError:
        return _not_found(request)


@router.delete(
    "/v1/orders/{order_id}",
    operation_id="cancelOrder",
    summary="Cancel an order",
    openapi_extra={"x-seeds-id": "A-07"},
)
async def cancel_order(request: Request, order_id: UUID, trader_id: TraderId) -> Any:
    orders = _orders(request)
    if orders is None:
        return _unavailable(request, "order-service is starting.")
    try:
        order = await orders.cancel(
            str(trader_id), str(order_id), correlation_id=correlation_id_var.get() or "none"
        )
    except OrderNotFoundError:
        return _not_found(request)
    except NotCancellableError as exc:
        return problem_response(
            409,
            "ORDER_NOT_CANCELLABLE",
            "Order cannot be cancelled",
            detail=str(exc),
            instance=request.url.path,
        )
    return JSONResponse(order_body(order), status_code=202)


@router.post(
    "/internal/v1/order-expiries",
    operation_id="requestOrderExpiry",
    summary="Request expiry of an unconfirmed order",
    openapi_extra={"x-seeds-id": "A-19"},
)
async def request_order_expiry(request: Request, body: ExpiryBody) -> Any:
    orders = _orders(request)
    if orders is None:
        return _unavailable(request, "order-service is starting.")
    accepted = await orders.request_expiry(str(body.order_id), str(body.trader_id), body.ticker)
    return JSONResponse(
        {
            "order_id": accepted.order_id,
            "requested_at": accepted.requested_at,
            "known_order": accepted.known_order,
        },
        status_code=202,
    )
