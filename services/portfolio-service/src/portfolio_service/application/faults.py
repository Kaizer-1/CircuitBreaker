"""The "Portfolio database degraded" fault (A-21/A-22; chaos scenarios section 3.4).

While it is on, every reservation (A-06) makes Postgres wait `delay_ms` inside its transaction, as
a struggling database would. order-service's reservation call times out after 500 ms, so its
circuit breaker opens and orders get 503 DEPENDENCY_UNAVAILABLE at once: the alert
OrderServiceCircuitOpen, which the SRE agent must NOT act on. Its tools could not help: the fault
is stored in this service's database, so a restart, a rollback or more replicas all find it again.
The only fix is to the data store, which the agent may never touch, so it escalates.

Every replica reads the stored fault when it starts and every `interval` seconds after; the replica
that handles A-21/A-22 sees the change at once. Switching it on and off is logged
(`FaultInjected` / `FaultCleared`); nothing about it is a metric (blind diagnosis).
"""

import asyncio
import logging
from datetime import datetime

from portfolio_service.ports.store import DatabaseFault, PortfolioStore

logger = logging.getLogger("faults")

DB_DEGRADED = "DB_DEGRADED"


class FaultAlreadyActiveError(Exception):
    def __init__(self, fault: DatabaseFault) -> None:
        super().__init__(fault.fault)
        self.fault = fault


class DatabaseDegraded:
    def __init__(self, store: PortfolioStore) -> None:
        self._store = store
        self.fault: DatabaseFault | None = None

    def delay_seconds(self) -> float:
        """How long each reservation waits in Postgres now (0 when the fault is off)."""
        return self.fault.delay_ms / 1000 if self.fault else 0.0

    async def refresh(self) -> None:
        async with self._store.transaction() as tx:
            self.fault = await tx.database_fault()

    async def start(self, delay_ms: int, now: datetime) -> DatabaseFault:
        fault = DatabaseFault(DB_DEGRADED, delay_ms, now)
        async with self._store.transaction() as tx:
            if not await tx.set_database_fault(fault):
                existing = await tx.database_fault()
                raise FaultAlreadyActiveError(existing or fault)
        self.fault = fault
        logger.info("FaultInjected", extra={"fault": DB_DEGRADED, "delay_ms": delay_ms})
        return fault

    async def stop(self) -> bool:
        async with self._store.transaction() as tx:
            cleared = await tx.clear_database_fault()
        self.fault = None
        if cleared:
            logger.info("FaultCleared", extra={"fault": DB_DEGRADED})
        return cleared

    async def watch(self, interval: float = 2.0) -> None:
        """Pick up a change made through another replica (or before a restart)."""
        while True:
            try:
                await self.refresh()
            except Exception:
                logger.exception("could not read the chaos fault; keeping the last known state")
            await asyncio.sleep(interval)
