"""FaultHooks over HTTP: A-12/A-13 (market-data), A-21/A-22 (portfolio), A-23/A-24 (bot-traders).

Each hook answers 201 when switched on, 204 when switched off, 404 when it was not on; anything
else (or no answer) is TargetUnavailableError.
"""

from typing import Any

import httpx

from chaos_service.ports.targets import TargetUnavailableError

PATH = "/internal/v1/faults"


class HttpFaultHooks:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        market_data_url: str,
        portfolio_url: str,
        bot_traders_url: str,
    ) -> None:
        self._client = client
        self._market_data = market_data_url.rstrip("/")
        self._portfolio = portfolio_url.rstrip("/")
        self._bots = bot_traders_url.rstrip("/")

    async def _on(self, base: str, body: dict[str, Any]) -> None:
        try:
            response = await self._client.post(f"{base}{PATH}", json=body)
        except httpx.HTTPError as exc:
            raise TargetUnavailableError(f"{base} did not answer: {type(exc).__name__}") from exc
        if response.status_code != 201:
            raise TargetUnavailableError(f"{base} answered {response.status_code}: {response.text}")

    async def _off(self, base: str) -> bool:
        try:
            response = await self._client.delete(f"{base}{PATH}")
        except httpx.HTTPError as exc:
            raise TargetUnavailableError(f"{base} did not answer: {type(exc).__name__}") from exc
        if response.status_code == 204:
            return True
        if response.status_code == 404:
            return False
        raise TargetUnavailableError(f"{base} answered {response.status_code}: {response.text}")

    async def memory_leak_on(self, rate_mb_per_minute: int) -> None:
        await self._on(
            self._market_data, {"fault": "MEMORY_LEAK", "rate_mb_per_minute": rate_mb_per_minute}
        )

    async def memory_leak_off(self) -> bool:
        return await self._off(self._market_data)

    async def database_degraded_on(self, delay_ms: int) -> None:
        await self._on(self._portfolio, {"fault": "DB_DEGRADED", "delay_ms": delay_ms})

    async def database_degraded_off(self) -> bool:
        return await self._off(self._portfolio)

    async def panic_sell_on(self, requests_per_second: int, duration_seconds: int) -> None:
        body = {
            "fault": "PANIC_SELL",
            "requests_per_second": requests_per_second,
            "duration_seconds": duration_seconds,
        }
        await self._on(self._bots, body)

    async def panic_sell_off(self) -> bool:
        return await self._off(self._bots)
