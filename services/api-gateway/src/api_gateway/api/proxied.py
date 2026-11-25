"""Every public route the gateway proxies to the service that owns it (spec `x-upstream`).

Each route names who may call it, as in api/api-gateway.yaml ("Roles: ..."): trading routes for
traders and bots, the market and incident views for any signed-in role, chaos and agent controls
for the admin. Started as the generated stubs (tools/gen_stubs.py); owned by this service now.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from api_gateway.adapters.proxy import proxy_request
from api_gateway.api.auth import ADMIN, ANY_ROLE, TRADING
from api_gateway.domain.sessions import Principal

Trading = Annotated[Principal, Depends(TRADING)]
AnyRole = Annotated[Principal, Depends(ANY_ROLE)]
Admin = Annotated[Principal, Depends(ADMIN)]

router = APIRouter()


@router.get(
    "/v1/orders",
    operation_id="listMyOrders",
    summary="List my orders",
    openapi_extra={"x-seeds-id": "Q-07"},
)
async def list_my_orders(request: Request, principal: Trading) -> Response:
    return await proxy_request(request, "order-service", principal)


@router.post(
    "/v1/orders",
    operation_id="placeOrder",
    summary="Place an order",
    openapi_extra={"x-seeds-id": "A-05"},
)
async def place_order(request: Request, principal: Trading) -> Response:
    return await proxy_request(request, "order-service", principal)


@router.get(
    "/v1/orders/{order_id}",
    operation_id="getOrder",
    summary="Get one of my orders",
    openapi_extra={"x-seeds-id": "Q-08"},
)
async def get_order(request: Request, principal: Trading) -> Response:
    return await proxy_request(request, "order-service", principal)


@router.delete(
    "/v1/orders/{order_id}",
    operation_id="cancelOrder",
    summary="Cancel an order",
    openapi_extra={"x-seeds-id": "A-07"},
)
async def cancel_order(request: Request, principal: Trading) -> Response:
    return await proxy_request(request, "order-service", principal)


@router.get(
    "/v1/instruments",
    operation_id="listInstruments",
    summary="List instruments with last price and change",
    openapi_extra={"x-seeds-id": "Q-02"},
)
async def list_instruments(request: Request, principal: AnyRole) -> Response:
    return await proxy_request(request, "market-data-service", principal)


@router.get(
    "/v1/market/{ticker}/candles",
    operation_id="getCandles",
    summary="Get candles for a ticker",
    openapi_extra={"x-seeds-id": "Q-03"},
)
async def get_candles(request: Request, principal: AnyRole) -> Response:
    return await proxy_request(request, "market-data-service", principal)


@router.get(
    "/v1/market/{ticker}/order-book",
    operation_id="getOrderBook",
    summary="Get order-book depth for a ticker",
    openapi_extra={"x-seeds-id": "Q-04"},
)
async def get_order_book(request: Request, principal: AnyRole) -> Response:
    return await proxy_request(request, "market-data-service", principal)


@router.get(
    "/v1/market/{ticker}/trades",
    operation_id="getRecentTrades",
    summary="Get recent trades for a ticker",
    openapi_extra={"x-seeds-id": "Q-05"},
)
async def get_recent_trades(request: Request, principal: AnyRole) -> Response:
    return await proxy_request(request, "market-data-service", principal)


@router.get(
    "/v1/time-travel/{ticker}",
    operation_id="getMarketAt",
    summary="Get the market as it was at time T",
    openapi_extra={"x-seeds-id": "Q-12"},
)
async def get_market_at(request: Request, principal: AnyRole) -> Response:
    return await proxy_request(request, "market-data-service", principal)


@router.get(
    "/v1/portfolio",
    operation_id="getMyPortfolio",
    summary="Get my portfolio",
    openapi_extra={"x-seeds-id": "Q-09"},
)
async def get_my_portfolio(request: Request, principal: Trading) -> Response:
    return await proxy_request(request, "portfolio-service", principal)


@router.get(
    "/v1/leaderboard",
    operation_id="getLeaderboard",
    summary="Get the leaderboard",
    openapi_extra={"x-seeds-id": "Q-11"},
)
async def get_leaderboard(request: Request, principal: AnyRole) -> Response:
    return await proxy_request(request, "portfolio-service", principal)


@router.get(
    "/v1/incidents",
    operation_id="listIncidents",
    summary="List incidents",
    openapi_extra={"x-seeds-id": "Q-17"},
)
async def list_incidents(request: Request, principal: AnyRole) -> Response:
    return await proxy_request(request, "sre-agent", principal)


@router.get(
    "/v1/incidents/{incident_id}",
    operation_id="getIncident",
    summary="Get one incident with its timeline",
    openapi_extra={"x-seeds-id": "Q-18"},
)
async def get_incident(request: Request, principal: AnyRole) -> Response:
    return await proxy_request(request, "sre-agent", principal)


@router.post(
    "/v1/incidents/{incident_id}/decision",
    operation_id="decideRemediation",
    summary="Approve or decline a proposed remediation",
    openapi_extra={"x-seeds-id": "A-15"},
)
async def decide_remediation(request: Request, principal: Admin) -> Response:
    return await proxy_request(request, "sre-agent", principal)


@router.get(
    "/v1/agent/settings",
    operation_id="getAgentSettings",
    summary="Get agent settings",
    openapi_extra={"x-seeds-id": "Q-20"},
)
async def get_agent_settings(request: Request, principal: Admin) -> Response:
    return await proxy_request(request, "sre-agent", principal)


@router.put(
    "/v1/agent/settings",
    operation_id="updateAgentSettings",
    summary="Update agent settings",
    openapi_extra={"x-seeds-id": "A-14"},
)
async def update_agent_settings(request: Request, principal: Admin) -> Response:
    return await proxy_request(request, "sre-agent", principal)


@router.get(
    "/v1/chaos/scenarios",
    operation_id="listChaosScenarios",
    summary="List chaos scenarios",
    openapi_extra={"x-seeds-id": "Q-21"},
)
async def list_chaos_scenarios(request: Request, principal: Admin) -> Response:
    return await proxy_request(request, "chaos-service", principal)


@router.get(
    "/v1/chaos/experiments",
    operation_id="listChaosExperiments",
    summary="List chaos experiments",
    openapi_extra={"x-seeds-id": "Q-22"},
)
async def list_chaos_experiments(request: Request, principal: Admin) -> Response:
    return await proxy_request(request, "chaos-service", principal)


@router.post(
    "/v1/chaos/experiments",
    operation_id="startChaosExperiment",
    summary="Start a chaos experiment",
    openapi_extra={"x-seeds-id": "A-10"},
)
async def start_chaos_experiment(request: Request, principal: Admin) -> Response:
    return await proxy_request(request, "chaos-service", principal)


@router.get(
    "/v1/chaos/experiments/{experiment_id}",
    operation_id="getChaosExperiment",
    summary="Get one chaos experiment",
    openapi_extra={"x-seeds-id": "Q-23"},
)
async def get_chaos_experiment(request: Request, principal: Admin) -> Response:
    return await proxy_request(request, "chaos-service", principal)


@router.post(
    "/v1/chaos/experiments/{experiment_id}/stop",
    operation_id="stopChaosExperiment",
    summary="Stop a chaos experiment",
    openapi_extra={"x-seeds-id": "A-11"},
)
async def stop_chaos_experiment(request: Request, principal: Admin) -> Response:
    return await proxy_request(request, "chaos-service", principal)


@router.post(
    "/v1/chaos/reset",
    operation_id="resetChaos",
    summary="Reset all chaos",
    openapi_extra={"x-seeds-id": "A-20"},
)
async def reset_chaos(request: Request, principal: Admin) -> Response:
    return await proxy_request(request, "chaos-service", principal)
