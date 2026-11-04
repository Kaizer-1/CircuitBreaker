"""order-service's A-19, used by the fail-closed orphan sweeper (ADR-0017)."""

from typing import Protocol


class OrderService(Protocol):
    async def request_expiry(self, order_id: str, trader_id: str, ticker: str) -> None:
        """Ask order-service to publish an OrderExpiryRequested. Raises if it did not accept it."""
