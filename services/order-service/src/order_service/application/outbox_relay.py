"""The outbox relay (docs/03-architecture/04-event-sourcing-and-cqrs.md, "The outbox").

Every second it re-publishes outbox rows that are still unpublished 5 s after they were written
(the request path tries once at once). Two rules:

- It never publishes an OrderPlaced older than 60 s: that order becomes REJECTED (PUBLISH_TIMEOUT)
  and its row is dropped (ADR-0011). The trader learns quickly that it did not get through. The
  Kafka client may keep trying a send for up to its delivery timeout D (10 s) after the relay
  stopped waiting, so the relay starts no OrderPlaced send once the order is older than 60 s - D:
  nothing it hands over can reach the broker after 60 s (ADR-0017).
- A cancel or expiry is never published before its order's OrderPlaced.

A cancel or an expiry request has **no time limit**: it is retried until it is published, however
long Redpanda or this service was down, because until the engine answers it the trader's money stays
reserved (ADR-0017 note "No time limit for cancels and expiries"). Until Phase 4 the relay looked
back only 15 minutes, and a cancel left behind by a long outage kept its order resting for ever.
Since the outbox rows expire an hour after they are written (a TTL keeps the table small), two rules
keep that promise:

- a request still waiting after 20 minutes is written again at "now" (the same event) and the old
  row marked done, so a waiting request never reaches the TTL while the service runs;
- when the relay starts, it sends again the stored cancel of every open order that asked for one:
  that covers a service (or laptop) stopped for longer than the TTL. An expiry request lost the same
  way is asked again by portfolio-service's sweeper every 5 minutes (ADR-0017).

Several replicas may run it at once: a row can be published twice, and the engine drops the copy
by event_id.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from order_service.application.orders import must_wait
from order_service.domain.order import (
    FINISHED,
    PUBLISH_TIMEOUT_SECONDS,
    Order,
    OrderStatus,
    RejectionReason,
)
from order_service.ports.order_store import TOPIC, OrderStore, OutboxEntry
from order_service.ports.publisher import OrderPublisher
from order_service.runtime.events import format_timestamp, parse_timestamp

logger = logging.getLogger("outbox")

RETRY_AFTER_SECONDS = 5
# Well inside the outbox rows' 1-hour TTL (adapters/cassandra_store.py OUTBOX_TTL).
CARRY_FORWARD_AFTER = timedelta(minutes=20)
REQUESTS = {"OrderCancelRequested", "OrderExpiryRequested"}
DELIVERY_TIMEOUT_SECONDS = 10  # the publisher's delivery_timeout_seconds (config.py)
# A replica writes a row at its own clock. Rows are never written further than this in the past, so
# the store may stop re-reading empty minutes older than it. Every replica runs on the host's clock
# (Compose, kind); two minutes is ample.
CLOCK_MARGIN = timedelta(minutes=2)


class OutboxRelay:
    def __init__(
        self,
        store: OrderStore,
        publisher: OrderPublisher,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        publish_timeout_seconds: float = PUBLISH_TIMEOUT_SECONDS,
        delivery_timeout_seconds: float = DELIVERY_TIMEOUT_SECONDS,
        retry_after_seconds: float = RETRY_AFTER_SECONDS,
        on_rejected: Callable[[RejectionReason], None] = lambda reason: None,
        attempt_seconds: float = 2.0,
    ) -> None:
        self._attempt = attempt_seconds
        self._store = store
        self._publisher = publisher
        self._clock = clock
        self._timeout = timedelta(seconds=publish_timeout_seconds)
        self._last_send = timedelta(seconds=publish_timeout_seconds - delivery_timeout_seconds)
        self._retry_after = timedelta(seconds=retry_after_seconds)
        self._on_rejected = on_rejected
        self._resumed = False  # the open orders' pending cancels were sent again (start-up)

    async def run_once(self) -> int:
        """One pass over the outbox. Returns how many rows were published.

        First every rule that needs no broker (reject what is too old, drop what must never go out),
        so a hung Redpanda cannot delay a PUBLISH_TIMEOUT; then one bounded attempt per row.
        """
        now = self._clock()
        entries = await self._store.pending_outbox(format_timestamp(now - CLOCK_MARGIN))
        if not self._resumed:
            await self._resend_open_cancels(now, waiting={e.event.event_id for e in entries})
            self._resumed = True
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
                if order.status in FINISHED:
                    await self._store.drop_outbox(entry)  # nothing left to cancel
                    continue
            if entry.event_type in REQUESTS and now - written_at(entry) > CARRY_FORWARD_AFTER:
                # Written again before the row's TTL runs out: the request never expires.
                await self._store.requeue(replace(entry, written_at=format_timestamp(now)))
                await self._store.drop_outbox(entry)
                continue
            to_publish.append((entry, order))

        published = 0
        for entry, order in to_publish:
            if order is not None:
                order = await self._store.get(entry.order_id)  # an earlier row may have changed it
            written = parse_timestamp(entry.row_at)
            if must_wait(entry, order) or now - written < self._retry_after:
                continue
            if order is not None and entry.event_type == "OrderPlaced" and self._too_late(order):
                continue  # a send now could land after 60 s: wait for the rejection instead
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

    async def _resend_open_cancels(self, now: datetime, *, waiting: set[str]) -> None:
        """Start-up: the stored cancel of every open order that asked for one goes back in the
        outbox, unless it is still `waiting` there. Its row may have expired while the service was
        stopped; a copy the engine already has is dropped there by event_id. The next pass sends
        it."""
        for order in await self._store.open_orders():
            if not order.cancel_requested:
                continue
            event = await self._store.find_event(order.order_id, "OrderCancelRequested")
            if event is not None and event.event_id not in waiting:
                entry = OutboxEntry(order.order_id, order.ticker, event, format_timestamp(now))
                await self._store.requeue(entry)

    def _too_late(self, order: Order) -> bool:
        return self._clock() - parse_timestamp(order.placed_at) > self._last_send

    async def run(self, every_seconds: float = 1.0) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                logger.exception("outbox relay pass failed")
            await asyncio.sleep(every_seconds)


def written_at(entry: OutboxEntry) -> datetime:
    return parse_timestamp(entry.row_at)
