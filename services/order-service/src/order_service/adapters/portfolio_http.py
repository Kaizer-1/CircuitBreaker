"""portfolio-service's A-06 over HTTP, behind the circuit breaker (ADR-0011: 500 ms timeout).

The breaker's state and trips are exported as Prometheus metrics (see business_metrics.py):
`circuit_breaker_state{dependency="portfolio-service",state=...}` is 1 for the current state.
"""

import httpx

from order_service.application.circuit_breaker import CircuitBreaker, CircuitOpenError
from order_service.domain.order import OrderRequest, RejectionReason
from order_service.ports.portfolio import (
    DependencyUnavailableError,
    ReservationRefusedError,
    ReservationResult,
)
from order_service.runtime.jsonlog import correlation_id_var

REFUSALS = {
    "INSUFFICIENT_FUNDS": RejectionReason.INSUFFICIENT_FUNDS,
    "INSUFFICIENT_SHARES": RejectionReason.INSUFFICIENT_SHARES,
}


class HttpPortfolio:
    def __init__(
        self,
        base_url: str,
        client: httpx.AsyncClient,
        breaker: CircuitBreaker,
        *,
        timeout_seconds: float = 0.5,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/internal/v1/reservations"
        self._client = client
        self._breaker = breaker
        self._timeout = timeout_seconds

    async def reserve(
        self, order_id: str, trader_id: str, request: OrderRequest
    ) -> ReservationResult:
        try:
            self._breaker.before_call()
        except CircuitOpenError as exc:
            raise DependencyUnavailableError("portfolio-service circuit breaker is open") from exc
        # The caller's correlation ID, so portfolio-service logs the reservation under it too.
        correlation_id = correlation_id_var.get()
        try:
            response = await self._client.post(
                self._url,
                json={
                    "order_id": order_id,
                    "trader_id": trader_id,
                    "ticker": request.ticker,
                    "side": request.side.value,
                    "order_type": request.order_type.value,
                    "quantity": request.quantity,
                    "limit_price_paise": request.limit_price_paise,
                },
                headers={"X-Correlation-Id": correlation_id} if correlation_id else None,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            self._breaker.failed()
            raise DependencyUnavailableError(f"portfolio-service did not answer: {exc!r}") from exc
        if response.status_code >= 500:
            self._breaker.failed()
            raise DependencyUnavailableError(f"portfolio-service answered {response.status_code}")
        self._breaker.succeeded()  # it answered, even if the answer is "no"

        body = response.json()
        if response.status_code in (200, 201):
            return ReservationResult(body["reservation_id"], body["protection_price_paise"])
        reason = REFUSALS.get(body.get("code", ""), RejectionReason.VALIDATION_FAILED)
        raise ReservationRefusedError(reason, body.get("detail", "reservation refused"))
