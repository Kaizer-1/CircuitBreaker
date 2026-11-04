"""The outbox relay (docs/03-architecture/04-event-sourcing-and-cqrs.md, "The outbox").

Every second it re-publishes outbox rows that are still unpublished 5 s after they were written
(the request path tries once at once). Two rules:

- It never publishes an OrderPlaced older than 60 s: that order becomes REJECTED (PUBLISH_TIMEOUT)
  and its row is dropped (ADR-0011). The trader learns quickly that it did not get through.
- A cancel or expiry is never published before its order's OrderPlaced.

Several replicas may run it at once: a row can be published twice, and the engine drops the copy
by event_id. Rows older than the look-back window (15 min, Redpanda down that long) are not
retried: an order among them is later rejected through A-19, when portfolio asks for its expiry.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from order_service.application.orders import must_wait
from order_service.domain.order import PUBLISH_TIMEOUT_SECONDS, OrderStatus, RejectionReason
from order_service.ports.order_store import TOPIC, OrderStore
from order_service.ports.publisher import OrderPublisher
from order_service.runtime.events import format_timestamp, parse_timestamp

logger = logging.getLogger("outbox")

RETRY_AFTER_SECONDS = 5
LOOK_BACK = timedelta(minutes=15)


class OutboxRelay:
    def __init__(
        self,
        store: OrderStore,
        publisher: OrderPublisher,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        publish_timeout_seconds: float = PUBLISH_TIMEOUT_SECONDS,
        retry_after_seconds: float = RETRY_AFTER_SECONDS,
        on_rejected: Callable[[RejectionReason], None] = lambda reason: None,
        attempt_seconds: float = 2.0,
    ) -> None:
        self._attempt = attempt_seconds
        self._store = store
        self._publisher = publisher
        self._clock = clock
        self._timeout = timedelta(seconds=publish_timeout_seconds)
        self._retry_after = timedelta(seconds=retry_after_seconds)
        self._on_rejected = on_rejected

    async def run_once(self) -> int:
        """One pass over the outbox. Returns how many rows were published.

        First every rule that needs no broker (reject what is too old, drop what must never go out),
        so a hung Redpanda cannot delay a PUBLISH_TIMEOUT; then one bounded attempt per row.
        """
        now = self._clock()
        entries = await self._store.pending_outbox(format_timestamp(now - LOOK_BACK))
        to_publish = []
        for entry in entries:
            order = await self._store.get(entry.order_id)
            if entry.event_type == "OrderPlaced":
                if order is None or order.status is not OrderStatus.PENDING:
                    await self._store.drop_outbox(entry)  # rejected meanwhile: never publish
                    continue
                if now - parse_timestamp(order.placed_at) > self._timeout:
                    order.reject(RejectionReason.PUBLISH_TIMEOUT, format_timestamp(now))
                    await self._store.save_status(
                        [order], processed_event_id=None, cancel_flag=False
                    )
                    await self._store.drop_outbox(entry)
                    self._on_rejected(RejectionReason.PUBLISH_TIMEOUT)
                    logger.warning(
                        "OrderPlaced not published within 60 s: order rejected",
                        extra={"order_id": order.order_id},
                    )
                    continue
            elif order is not None and entry.event_type == "OrderCancelRequested":
                if order.status is OrderStatus.REJECTED:
                    await self._store.drop_outbox(entry)  # nothing left to cancel
                    continue
            to_publish.append((entry, order))

        published = 0
        for entry, order in to_publish:
            if order is not None:
                order = await self._store.get(entry.order_id)  # an earlier row may have changed it
            written = parse_timestamp(entry.event.occurred_at)
            if must_wait(entry, order) or now - written < self._retry_after:
                continue
            try:
                await asyncio.wait_for(
                    self._publisher.publish(TOPIC, entry.ticker, entry.event), self._attempt
                )
            except Exception as exc:
                logger.warning("relay publish failed", extra={"error": repr(exc)})
                return published  # the broker is down or slow: try again on the next pass
            await self._store.mark_published(entry)
            published += 1
        return published

    async def run(self, every_seconds: float = 1.0) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                logger.exception("outbox relay pass failed")
            await asyncio.sleep(every_seconds)
