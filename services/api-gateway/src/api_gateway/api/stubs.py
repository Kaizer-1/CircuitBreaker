"""Stub routes for every operation in api/api-gateway.yaml.

Generated once by tools/gen_stubs.py (contract first). From now on edit this file by hand:
replace a stub with the real route when the operation is built. tests/contract/ fails if this
file and the spec disagree.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from api_gateway.adapters.proxy import proxy_request
from api_gateway.runtime.problem import not_implemented

router = APIRouter()


@router.post(
    "/v1/sessions",
    operation_id="joinMarket",
    summary="Join the market as a trader",
    openapi_extra={"x-seeds-id": "A-01"},
)
async def join_market(request: Request) -> JSONResponse:
    return not_implemented(request, "A-01")


@router.post(
    "/v1/bot-sessions",
    operation_id="startBotSession",
    summary="Start a bot session",
    openapi_extra={"x-seeds-id": "A-03"},
)
async def start_bot_session(request: Request) -> JSONResponse:
    return not_implemented(request, "A-03")


@router.post(
    "/v1/admin/sessions",
    operation_id="startAdminSession",
    summary="Start an admin session",
    openapi_extra={"x-seeds-id": "A-04"},
)
async def start_admin_session(request: Request) -> JSONResponse:
    return not_implemented(request, "A-04")


@router.get(
    "/v1/session",
    operation_id="getCurrentSession",
    summary="Get my current session",
    openapi_extra={"x-seeds-id": "Q-01"},
)
async def get_current_session(request: Request) -> JSONResponse:
    return not_implemented(request, "Q-01")


@router.get(
    "/v1/orders",
    operation_id="listMyOrders",
    summary="List my orders",
    openapi_extra={"x-seeds-id": "Q-07"},
)
async def list_my_orders(request: Request) -> Response:
    return await proxy_request(request, "order-service")


@router.post(
    "/v1/orders",
    operation_id="placeOrder",
    summary="Place an order",
    openapi_extra={"x-seeds-id": "A-05"},
)
async def place_order(request: Request) -> Response:
    return await proxy_request(request, "order-service")


@router.get(
    "/v1/orders/{order_id}",
    operation_id="getOrder",
    summary="Get one of my orders",
    openapi_extra={"x-seeds-id": "Q-08"},
)
async def get_order(request: Request) -> Response:
    return await proxy_request(request, "order-service")


@router.delete(
    "/v1/orders/{order_id}",
    operation_id="cancelOrder",
    summary="Cancel an order",
    openapi_extra={"x-seeds-id": "A-07"},
)
async def cancel_order(request: Request) -> Response:
    return await proxy_request(request, "order-service")


@router.get(
    "/v1/instruments",
    operation_id="listInstruments",
    summary="List instruments with last price and change",
    openapi_extra={"x-seeds-id": "Q-02"},
)
async def list_instruments(request: Request) -> Response:
    return await proxy_request(request, "market-data-service")


@router.get(
    "/v1/market/{ticker}/candles",
    operation_id="getCandles",
    summary="Get candles for a ticker",
    openapi_extra={"x-seeds-id": "Q-03"},
)
async def get_candles(request: Request) -> Response:
    return await proxy_request(request, "market-data-service")


@router.get(
    "/v1/market/{ticker}/order-book",
    operation_id="getOrderBook",
    summary="Get order-book depth for a ticker",
    openapi_extra={"x-seeds-id": "Q-04"},
)
async def get_order_book(request: Request) -> Response:
    return await proxy_request(request, "market-data-service")


@router.get(
    "/v1/market/{ticker}/trades",
    operation_id="getRecentTrades",
    summary="Get recent trades for a ticker",
    openapi_extra={"x-seeds-id": "Q-05"},
)
async def get_recent_trades(request: Request) -> Response:
    return await proxy_request(request, "market-data-service")


@router.get(
    "/v1/time-travel/{ticker}",
    operation_id="getMarketAt",
    summary="Get the market as it was at time T",
    openapi_extra={"x-seeds-id": "Q-12"},
)
async def get_market_at(request: Request) -> Response:
    return await proxy_request(request, "market-data-service")


@router.get(
    "/v1/portfolio",
    operation_id="getMyPortfolio",
    summary="Get my portfolio",
    openapi_extra={"x-seeds-id": "Q-09"},
)
async def get_my_portfolio(request: Request) -> Response:
    return await proxy_request(request, "portfolio-service")


@router.get(
    "/v1/leaderboard",
    operation_id="getLeaderboard",
    summary="Get the leaderboard",
    openapi_extra={"x-seeds-id": "Q-11"},
)
async def get_leaderboard(request: Request) -> Response:
    return await proxy_request(request, "portfolio-service")


@router.get(
    "/v1/incidents",
    operation_id="listIncidents",
    summary="List incidents",
    openapi_extra={"x-seeds-id": "Q-17"},
)
async def list_incidents(request: Request) -> Response:
    return await proxy_request(request, "sre-agent")


@router.get(
    "/v1/incidents/{incident_id}",
    operation_id="getIncident",
    summary="Get one incident with its timeline",
    openapi_extra={"x-seeds-id": "Q-18"},
)
async def get_incident(request: Request) -> Response:
    return await proxy_request(request, "sre-agent")


@router.post(
    "/v1/incidents/{incident_id}/decision",
    operation_id="decideRemediation",
    summary="Approve or decline a proposed remediation",
    openapi_extra={"x-seeds-id": "A-15"},
)
async def decide_remediation(request: Request) -> Response:
    return await proxy_request(request, "sre-agent")


@router.get(
    "/v1/agent/settings",
    operation_id="getAgentSettings",
    summary="Get agent settings",
    openapi_extra={"x-seeds-id": "Q-20"},
)
async def get_agent_settings(request: Request) -> Response:
    return await proxy_request(request, "sre-agent")


@router.put(
    "/v1/agent/settings",
    operation_id="updateAgentSettings",
    summary="Update agent settings",
    openapi_extra={"x-seeds-id": "A-14"},
)
async def update_agent_settings(request: Request) -> Response:
    return await proxy_request(request, "sre-agent")


@router.get(
    "/v1/chaos/scenarios",
    operation_id="listChaosScenarios",
    summary="List chaos scenarios",
    openapi_extra={"x-seeds-id": "Q-21"},
)
async def list_chaos_scenarios(request: Request) -> Response:
    return await proxy_request(request, "chaos-service")


@router.get(
    "/v1/chaos/experiments",
    operation_id="listChaosExperiments",
    summary="List chaos experiments",
    openapi_extra={"x-seeds-id": "Q-22"},
)
async def list_chaos_experiments(request: Request) -> Response:
    return await proxy_request(request, "chaos-service")


@router.post(
    "/v1/chaos/experiments",
    operation_id="startChaosExperiment",
    summary="Start a chaos experiment",
    openapi_extra={"x-seeds-id": "A-10"},
)
async def start_chaos_experiment(request: Request) -> Response:
    return await proxy_request(request, "chaos-service")


@router.get(
    "/v1/chaos/experiments/{experiment_id}",
    operation_id="getChaosExperiment",
    summary="Get one chaos experiment",
    openapi_extra={"x-seeds-id": "Q-23"},
)
async def get_chaos_experiment(request: Request) -> Response:
    return await proxy_request(request, "chaos-service")


@router.post(
    "/v1/chaos/experiments/{experiment_id}/stop",
    operation_id="stopChaosExperiment",
    summary="Stop a chaos experiment",
    openapi_extra={"x-seeds-id": "A-11"},
)
async def stop_chaos_experiment(request: Request) -> Response:
    return await proxy_request(request, "chaos-service")
