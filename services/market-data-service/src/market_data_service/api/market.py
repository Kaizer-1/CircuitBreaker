"""Q-02 to Q-05 (api/market-data-service.yaml) and the Q-06 WebSocket (api/README.md).

Thin routers: read the projection (memory) or the store (Cassandra) and shape the answer.
"""

import asyncio
import contextlib
import json
import logging
import time
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from market_data_service.application.hub import MarketHub, Subscriber
from market_data_service.application.projection import MarketProjection
from market_data_service.application.views import (
    candle_view,
    depth_view,
    instrument_view,
    trade_view,
)
from market_data_service.domain.candles import INTERVALS, bucket_of
from market_data_service.domain.instruments import TICKERS
from market_data_service.ports.market_store import MarketStore
from market_data_service.runtime.events import parse_timestamp
from market_data_service.runtime.problem import problem_response

logger = logging.getLogger("api")

Ticker = Literal["CHAIWALA", "JUGAAD", "DABBA", "MONSOON", "FILMY"]
Interval = Literal["5s", "1m", "5m"]
PING_EVERY_SECONDS = 15.0  # api/README.md: server pings every 15 s,
PONG_TIMEOUT_SECONDS = 45.0  # and closes the connection after 45 s without a pong

router = APIRouter()


def _projection(request: Request) -> MarketProjection | None:
    projection: MarketProjection | None = getattr(request.app.state, "projection", None)
    return projection if projection is not None and projection.ready else None


def _starting(request: Request) -> JSONResponse:
    return problem_response(
        503,
        "DEPENDENCY_UNAVAILABLE",
        "Market data is starting",
        detail="The market is still being rebuilt from the event store.",
        instance=request.url.path,
    )


def _store(request: Request) -> MarketStore | None:
    store: MarketStore | None = getattr(request.app.state, "store", None)
    return store


@router.get(
    "/v1/instruments",
    operation_id="listInstruments",
    summary="List instruments with last price and change",
    openapi_extra={"x-seeds-id": "Q-02"},
)
async def list_instruments(request: Request) -> JSONResponse:
    projection = _projection(request)
    if projection is None:
        return _starting(request)
    now = projection.now_ms()
    today = datetime.fromtimestamp(now / 1000, UTC).date()
    return JSONResponse(
        {"instruments": [instrument_view(m, today, now) for m in projection.markets.values()]}
    )


@router.get(
    "/v1/market/{ticker}/candles",
    operation_id="getCandles",
    summary="Get candles for a ticker",
    openapi_extra={"x-seeds-id": "Q-03"},
)
async def get_candles(
    request: Request,
    ticker: Ticker,
    interval: Interval,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> JSONResponse:
    store, projection = _store(request), _projection(request)
    if store is None or projection is None:
        return _starting(request)
    step = INTERVALS[interval]
    try:
        to_ms = _ms(to) if to else projection.now_ms()
        from_ms = _ms(from_) if from_ else bucket_of(to_ms, step) - 100 * step
    except ValueError:
        return problem_response(
            422,
            "VALIDATION_FAILED",
            "Validation failed",
            detail="`from` and `to` must be ISO-8601 times.",
            instance=request.url.path,
        )
    candles = await store.candles(ticker, interval, from_ms, to_ms, limit)
    return JSONResponse(
        {"ticker": ticker, "interval": interval, "candles": [candle_view(c) for c in candles]}
    )


@router.get(
    "/v1/market/{ticker}/order-book",
    operation_id="getOrderBook",
    summary="Get order-book depth for a ticker",
    openapi_extra={"x-seeds-id": "Q-04"},
)
async def get_order_book(
    request: Request, ticker: Ticker, depth: Annotated[int, Query(ge=1, le=50)] = 10
) -> JSONResponse:
    projection = _projection(request)
    if projection is None:
        return _starting(request)
    return JSONResponse(depth_view(projection.markets[ticker], depth, projection.now_ms()))


@router.get(
    "/v1/market/{ticker}/trades",
    operation_id="getRecentTrades",
    summary="Get recent trades for a ticker",
    openapi_extra={"x-seeds-id": "Q-05"},
)
async def get_recent_trades(
    request: Request, ticker: Ticker, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> JSONResponse:
    store = _store(request)
    if store is None or _projection(request) is None:
        return _starting(request)
    trades = await store.trades(ticker, limit)
    return JSONResponse({"ticker": ticker, "trades": [trade_view(t) for t in trades]})


def _ms(value: str) -> int:
    return int(parse_timestamp(value).timestamp() * 1000)


# ---------------------------------------------------------------------- Q-06 WebSocket


@router.websocket("/v1/ws/market/{ticker}")
async def stream_market(websocket: WebSocket, ticker: str) -> None:
    await websocket.accept()
    hub: MarketHub | None = getattr(websocket.app.state, "hub", None)
    if ticker not in TICKERS:
        await websocket.close(code=4404, reason="unknown ticker")
        return
    if hub is None or not hub.ready:
        await websocket.close(code=1011, reason="market data is starting")
        return
    snapshot, subscriber = hub.subscribe(ticker)
    settings = websocket.app.state.settings
    try:
        await websocket.send_json(snapshot)
        await _serve(
            websocket, subscriber, settings.ws_ping_seconds, settings.ws_pong_timeout_seconds
        )
    except WebSocketDisconnect:
        pass
    finally:
        hub.unsubscribe(subscriber)


async def _serve(
    websocket: WebSocket, subscriber: Subscriber, ping_every: float, pong_timeout: float
) -> None:
    last_pong = time.monotonic()

    async def send() -> None:
        while True:
            message = await subscriber.queue.get()
            if subscriber.lagging:
                await websocket.close(code=1011, reason="too far behind: reconnect for a snapshot")
                return
            await websocket.send_json(message)

    async def receive() -> None:
        nonlocal last_pong
        while True:
            try:
                message: Any = json.loads(await websocket.receive_text())
            except ValueError:
                continue  # not JSON: ignored, like an unknown message type
            kind = message.get("type") if isinstance(message, dict) else None
            if kind == "pong":
                last_pong = time.monotonic()
            elif kind == "subscribe":
                wanted = (message.get("data") or {}).get("intervals") or []
                chosen = [i for i in wanted if i in INTERVALS]
                if chosen:
                    subscriber.intervals = set(chosen)

    async def ping() -> None:
        while True:
            await asyncio.sleep(ping_every)
            if time.monotonic() - last_pong > pong_timeout:
                await websocket.close(code=1000, reason="no pong")
                return
            await websocket.send_json({"type": "ping"})

    tasks = [asyncio.create_task(t()) for t in (send, receive, ping)]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, WebSocketDisconnect):
                logger.warning("market stream ended", extra={"error": repr(exc)})
    finally:
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
