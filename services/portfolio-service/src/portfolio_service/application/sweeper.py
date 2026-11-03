"""The orphan sweeper, policy P-15 (ADR-0017: fail-closed release of orphaned reservations).

Every cycle it finds reservations that have been ACTIVE for 120 s without an OrderAccepted and asks
order-service (A-19) to send an expiry request to the matching engine. It never releases money
itself: the release happens later, only when the engine's answer (OrderCancelled ORDER_EXPIRED or
OrderExpired) arrives on matching.v1. If order-service does not answer, the next cycle asks again.

`can_sweep(ticker)` lets the caller skip tickers whose matching.v1 partition this replica does not
own or has not fully read: an unread OrderAccepted must not look like an orphan.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from portfolio_service.domain.model import ReservationStatus
from portfolio_service.ports.order_service import OrderService
from portfolio_service.ports.store import PortfolioStore
from portfolio_service.runtime.events import format_timestamp

logger = logging.getLogger("sweeper")

ORPHAN_AFTER_SECONDS = 120
SWEEP_EVERY_SECONDS = 10


class OrphanSweeper:
    def __init__(
        self,
        store: PortfolioStore,
        order_service: OrderService,
        *,
        can_sweep: Callable[[str], bool],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        orphan_after_seconds: float = ORPHAN_AFTER_SECONDS,
        on_requested: Callable[[], None] = lambda: None,
    ) -> None:
        self._store = store
        self._order_service = order_service
        self._can_sweep = can_sweep
        self._clock = clock
        self._orphan_after = timedelta(seconds=orphan_after_seconds)
        self._on_requested = on_requested

    async def sweep_once(self) -> int:
        """Ask for an expiry of every orphan found now. Returns how many requests were accepted."""
        now = self._clock()
        async with self._store.transaction() as tx:
            candidates = await tx.unconfirmed_reservations(
                format_timestamp(now - self._orphan_after)
            )
        requested = 0
        for r in candidates:
            if not self._can_sweep(r.ticker):
                continue
            try:
                await self._order_service.request_expiry(r.order_id, r.trader_id, r.ticker)
            except Exception as exc:  # order-service down or failing: try again next cycle
                logger.warning(
                    "expiry request failed, will retry",
                    extra={"order_id": r.order_id, "error": repr(exc)},
                )
                continue
            async with self._store.transaction() as tx:
                fresh = await tx.reservation(r.order_id, lock=True)
                if fresh is not None and fresh.status is ReservationStatus.ACTIVE:
                    fresh.expiry_requested_at = format_timestamp(now)  # local event ExpiryRequested
                    await tx.save_reservation(fresh)
            requested += 1
            self._on_requested()
            logger.info(
                "expiry requested for an orphaned reservation", extra={"order_id": r.order_id}
            )
        return requested

    async def run(self, every_seconds: float = SWEEP_EVERY_SECONDS) -> None:
        while True:
            try:
                await self.sweep_once()
            except Exception:
                logger.exception("sweep failed")
            await asyncio.sleep(every_seconds)
