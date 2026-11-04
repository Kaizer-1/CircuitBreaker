"""portfolio-service's A-06 reservation (ADR-0011: synchronous, idempotent per order_id)."""

from dataclasses import dataclass
from typing import Protocol

from order_service.domain.order import OrderRequest, RejectionReason


@dataclass(frozen=True)
class ReservationResult:
    reservation_id: str
    protection_price_paise: int | None  # MARKET orders: copied into OrderPlaced


class ReservationRefusedError(Exception):
    """422 from portfolio-service: not enough cash (no margin) or shares (no short selling)."""

    def __init__(self, reason: RejectionReason, detail: str) -> None:
        super().__init__(detail)
        self.reason, self.detail = reason, detail


class DependencyUnavailableError(Exception):
    """portfolio-service (or our own store) is down, slow, or its circuit breaker is open."""


class Portfolio(Protocol):
    async def reserve(
        self, order_id: str, trader_id: str, request: OrderRequest
    ) -> ReservationResult: ...
