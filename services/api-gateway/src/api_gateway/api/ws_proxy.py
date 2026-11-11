"""The WebSocket channels (api/README.md "WebSocket channels"), proxied to their owning service.

Browsers cannot set headers on a WebSocket request, so the token comes as `?access_token=`. The
gateway verifies it and checks the channel's role, then opens the same channel on the upstream
service with identity headers it builds itself from the token (a client's own X-Trader-Id is never
forwarded, so a trader can only ever open their own portfolio stream). The token is not passed
on, and never logged: access logs record the path only. After that the gateway is a pipe: every
message is relayed both ways, and an upstream close code (e.g. 4404) reaches the client as is.

Close codes: 4401 no valid token, 4403 wrong role, 1011 the upstream is not reachable. Passed on
from the upstream as they are: 1000, 1013 ("try again later": a replay replica that is full) and
every 4xxx code; any other upstream code becomes 1011.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Protocol
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import APIRouter, WebSocket

from api_gateway.domain.sessions import InvalidTokenError, Principal, Role, Sessions

logger = logging.getLogger("ws-proxy")

router = APIRouter()


class Upstream(Protocol):
    """An open WebSocket to a service."""

    close_code: int | None

    async def send(self, message: str | bytes) -> None: ...

    def __aiter__(self) -> AsyncIterator[str | bytes]: ...


Connect = Callable[[str, dict[str, str]], AbstractAsyncContextManager[Upstream]]


@asynccontextmanager
async def connect_upstream(url: str, headers: dict[str, str]) -> AsyncIterator[Upstream]:
    import websockets

    async with websockets.connect(
        url, additional_headers=headers, open_timeout=5, ping_interval=None, max_size=2**20
    ) as ws:
        yield ws  # type: ignore[misc]


def _ws_base(http_base: str) -> str:
    return "ws" + http_base.removeprefix("http") if http_base.startswith("http") else http_base


async def _relay(client: WebSocket, upstream: Upstream) -> None:
    async def to_upstream() -> None:
        while True:
            message = await client.receive()
            if message["type"] == "websocket.disconnect":
                return
            data = message.get("text") if message.get("text") is not None else message.get("bytes")
            if data is not None:
                await upstream.send(data)

    async def to_client() -> int:
        with contextlib.suppress(Exception):  # a closed upstream ends the iteration or raises
            async for message in upstream:
                if isinstance(message, str):
                    await client.send_text(message)
                else:
                    await client.send_bytes(message)
        code = upstream.close_code
        passed_on = code is not None and (code in (1000, 1013) or 4000 <= code < 5000)
        return code if passed_on and code is not None else 1011

    up, down = asyncio.create_task(to_upstream()), asyncio.create_task(to_client())
    done, pending = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    if down in done and not down.cancelled() and down.exception() is None:
        with contextlib.suppress(Exception):
            await client.close(code=down.result())


async def proxy_websocket(websocket: WebSocket, upstream: str, roles: tuple[Role, ...]) -> None:
    await websocket.accept()
    sessions: Sessions = websocket.app.state.sessions
    try:
        principal: Principal = sessions.verify(websocket.query_params.get("access_token", ""))
    except InvalidTokenError:
        await websocket.close(code=4401, reason="sign in first")
        return
    if roles and principal.role not in roles:
        await websocket.close(code=4403, reason="not allowed for this role")
        return
    base = websocket.app.state.settings.service_upstreams.get(upstream)
    if base is None:
        await websocket.close(code=1011, reason=f"{upstream} is not configured")
        return

    query = urlencode(
        [(k, v) for k, v in websocket.query_params.multi_items() if k != "access_token"]
    )
    url = f"{_ws_base(base)}{websocket.url.path}" + (f"?{query}" if query else "")
    headers = {
        "X-Role": principal.role.value,
        "X-Nickname": principal.nickname,
        "X-Correlation-Id": str(uuid4()),
    }
    if principal.trader_id is not None:
        headers["X-Trader-Id"] = principal.trader_id
    connect: Connect = getattr(websocket.app.state, "ws_connect", connect_upstream)
    gauge: Any = websocket.app.state.business_metrics.ws_connections.labels(websocket.url.path)
    try:
        async with connect(url, headers) as upstream_ws:
            gauge.inc()
            try:
                await _relay(websocket, upstream_ws)
            finally:
                gauge.dec()
    except Exception as exc:
        logger.warning(
            "upstream WebSocket unavailable",
            extra={"upstream": upstream, "path": websocket.url.path, "error": type(exc).__name__},
        )
        with contextlib.suppress(Exception):
            await websocket.close(code=1011, reason=f"{upstream} is not reachable")


# Each channel with the roles api/README.md gives it.


@router.websocket("/v1/ws/market/{ticker}")
async def market_stream(websocket: WebSocket, ticker: str) -> None:
    """Q-06, any signed-in role."""
    await proxy_websocket(websocket, "market-data-service", ())


@router.websocket("/v1/ws/portfolio")
async def portfolio_stream(websocket: WebSocket) -> None:
    """Q-10, traders and bots (an admin has no portfolio)."""
    await proxy_websocket(websocket, "portfolio-service", (Role.TRADER, Role.BOT))


@router.websocket("/v1/ws/replay/{ticker}")
async def replay_stream(websocket: WebSocket, ticker: str) -> None:
    """Q-13, any signed-in role (market-data builds it in Phase 4)."""
    await proxy_websocket(websocket, "market-data-service", ())


@router.websocket("/v1/ws/incidents")
async def incident_stream(websocket: WebSocket) -> None:
    """Q-19, any signed-in role (sre-agent builds it in Phase 7)."""
    await proxy_websocket(websocket, "sre-agent", ())
