"""The control room's vitals (owner, 2026-11-27): a few numbers pushed on the incident feed (Q-19)
every poll as `{"type": "vitals"}`, so the audience sees a fault's impact and the agent's fix on
the same screen: order-service's request rate, error ratio, p95 latency and replicas, and how full
market-data-service's memory is.

They are read from Prometheus with the same queries the evidence collector uses, so the strip shows
what the agent sees. They are for screens only: this publisher writes to the feed and nothing else,
and nothing in the agent reads the feed, so its own vitals can never come back to it as evidence.
If Prometheus does not answer, nothing is sent: the screen greys the strip out after 15 s rather
than show old numbers as current.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from sre_agent.application.collector import Queries
from sre_agent.application.feed import FeedHub
from sre_agent.domain.incident import iso
from sre_agent.ports.observability import Metrics

logger = logging.getLogger("vitals")
STALE_SECONDS = 15  # the screen greys the strip out after this long without vitals
ORDERS = "order-service"
MARKET_DATA = "market-data-service"


class VitalsPublisher:
    def __init__(
        self,
        metrics: Metrics,
        feed: FeedHub,
        namespace: str,
        now: Callable[[], datetime],
        every_seconds: float = 5.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._metrics, self._feed, self._ns = metrics, feed, namespace
        self._now, self._every, self._sleep = now, every_seconds, sleep
        self._last: dict[str, Any] | None = None
        self._last_at: datetime | None = None

    def current(self) -> dict[str, Any] | None:
        """The last message, for a screen that just connected; None once it is stale."""
        if self._last is None or self._last_at is None:
            return None
        fresh = (self._now() - self._last_at).total_seconds() <= STALE_SECONDS
        return self._last if fresh else None

    async def read(self) -> dict[str, Any]:
        orders, market = Queries(self._ns, ORDERS), Queries(self._ns, MARKET_DATA)
        where = f'namespace="{self._ns}",deployment="{ORDERS}"'
        rate, ratio, p95, desired, ready, memory = await asyncio.gather(
            self._metrics.value(orders.rate()),
            self._metrics.value(orders.error_ratio),
            self._metrics.value(orders.p95),
            self._metrics.value(f"max(kube_deployment_spec_replicas{{{where}}})"),
            self._metrics.value(f"max(kube_deployment_status_replicas_ready{{{where}}})"),
            self._metrics.value(market.memory_of_limit),
        )
        return {
            "at": iso(self._now()),
            "service": ORDERS,
            "requests_per_second": _round(rate, 2),
            "error_ratio": _round(ratio, 4),
            "p95_seconds": _round(p95, 3),
            "replicas": {
                "desired": None if desired is None else int(desired),
                "ready": None if ready is None else int(ready),
            },
            "market_data_memory_of_limit": _round(memory, 3),
        }

    async def publish_once(self) -> bool:
        try:
            data = await self.read()
        except Exception as exc:  # Prometheus down or slow: send nothing, the screen goes stale
            logger.debug("no vitals this time", extra={"error": type(exc).__name__})
            return False
        self._last, self._last_at = {"type": "vitals", "data": data}, self._now()
        self._feed.publish("vitals", data)
        return True

    async def run(self) -> None:
        while True:
            await self.publish_once()
            await self._sleep(self._every)


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)
