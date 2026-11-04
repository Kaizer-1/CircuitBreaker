"""order-service's A-19 over HTTP: POST /internal/v1/order-expiries (api/order-service.yaml)."""

import httpx


class HttpOrderService:
    def __init__(
        self, base_url: str, client: httpx.AsyncClient, timeout_seconds: float = 2.0
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/internal/v1/order-expiries"
        self._client = client
        self._timeout = timeout_seconds

    async def request_expiry(self, order_id: str, trader_id: str, ticker: str) -> None:
        response = await self._client.post(
            self._url,
            json={"order_id": order_id, "trader_id": trader_id, "ticker": ticker},
            timeout=self._timeout,
        )
        if response.status_code != 202:
            raise RuntimeError(
                f"order-service answered {response.status_code} to an expiry request"
            )
