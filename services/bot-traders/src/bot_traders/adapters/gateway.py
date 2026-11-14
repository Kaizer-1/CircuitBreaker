"""The Exchange port over the public API on api-gateway (bots get no shortcuts), including the
Q-10 portfolio WebSocket (bots are allowed on it, like traders)."""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import httpx

from bot_traders.domain.market import Place
from bot_traders.ports.exchange import OrderRefusedError, Session, SessionExpiredError

RETRIES_ON_429 = 3


class GatewayExchange:
    def __init__(self, base_url: str, bot_secret: str, client: httpx.AsyncClient) -> None:
        self._base = base_url.rstrip("/")
        self._secret = bot_secret
        self._client = client

    async def _call(
        self,
        method: str,
        path: str,
        session: Session | None = None,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        all_headers = dict(headers or {})
        if session is not None:
            all_headers["Authorization"] = f"Bearer {session.token}"
        for _ in range(RETRIES_ON_429):
            response = await self._client.request(
                method,
                f"{self._base}{path}",
                json=json,
                params=params,
                headers=all_headers,
            )
            if response.status_code != 429:
                break
            await asyncio.sleep(float(response.headers.get("Retry-After", "1")))  # the rate limit
        if response.status_code == 401 and session is not None:
            raise SessionExpiredError(f"{session.account}: token refused on {method} {path}")
        return response

    async def login(self, account: str) -> Session:
        response = await self._call(
            "POST",
            "/v1/bot-sessions",
            json={"account_name": account},
            headers={"X-Bot-Secret": self._secret},
        )
        response.raise_for_status()
        body = response.json()
        return Session(account, body["trader_id"], body["access_token"])

    async def place(self, session: Session, order: Place) -> dict[str, Any]:
        body: dict[str, Any] = {
            "ticker": order.ticker,
            "side": order.side,
            "order_type": order.order_type,
            "quantity": order.quantity,
        }
        if order.limit_price_paise is not None:
            body["limit_price_paise"] = order.limit_price_paise
        # A retried request must not become a second order. If the answer is lost (a timeout, a
        # dropped connection) the order may exist anyway: asking again with the same key returns
        # it, so the bot knows about it instead of leaving an order it never cancels (Phase 4:
        # such orphans piled up in the book during an outage).
        key = {"Idempotency-Key": str(uuid4())}
        try:
            response = await self._call("POST", "/v1/orders", session, json=body, headers=key)
        except httpx.TransportError:
            response = await self._call("POST", "/v1/orders", session, json=body, headers=key)
        if response.status_code == 422:
            problem = response.json()
            raise OrderRefusedError(
                problem.get("code", "VALIDATION_FAILED"), problem.get("detail", "")
            )
        response.raise_for_status()
        placed: dict[str, Any] = response.json()
        return placed

    async def cancel(self, session: Session, order_id: str) -> None:
        response = await self._call("DELETE", f"/v1/orders/{order_id}", session)
        if response.status_code in (404, 409):
            return  # already finished (filled, cancelled or never resting): nothing to do
        response.raise_for_status()

    async def open_orders(self, session: Session) -> list[dict[str, Any]]:
        response = await self._call(
            "GET",
            "/v1/orders",
            session,
            params={"status": "open", "limit": 100},
        )
        response.raise_for_status()
        items: list[dict[str, Any]] = response.json()["items"]
        return items

    async def portfolio(self, session: Session) -> dict[str, Any]:
        response = await self._call("GET", "/v1/portfolio", session)
        response.raise_for_status()
        body: dict[str, Any] = response.json()
        return body

    async def depth(self, session: Session, ticker: str) -> dict[str, Any]:
        response = await self._call(
            "GET", f"/v1/market/{ticker}/order-book", session, params={"depth": 1}
        )
        response.raise_for_status()
        body: dict[str, Any] = response.json()
        return body

    async def instruments(self, session: Session) -> list[dict[str, Any]]:
        response = await self._call("GET", "/v1/instruments", session)
        response.raise_for_status()
        items: list[dict[str, Any]] = response.json()["instruments"]
        return items

    async def portfolio_stream(self, session: Session) -> AsyncIterator[dict[str, Any]]:
        import websockets

        url = self._base.replace("http", "ws", 1) + "/v1/ws/portfolio"
        try:
            async with websockets.connect(
                f"{url}?access_token={session.token}", open_timeout=5, ping_interval=None
            ) as ws:
                async for raw in ws:
                    message: dict[str, Any] = json.loads(raw)
                    if message.get("type") == "ping":
                        await ws.send('{"type":"pong"}')  # the api/README.md heartbeat
                        continue
                    yield message
        except websockets.ConnectionClosed as exc:
            if exc.rcvd is not None and exc.rcvd.code == 4401:
                raise SessionExpiredError(f"{session.account}: stream refused the token") from exc
            raise
