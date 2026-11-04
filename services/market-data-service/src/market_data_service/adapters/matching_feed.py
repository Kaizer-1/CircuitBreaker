"""The live matching.v1 feed: every partition, no consumer group, from the end.

Each market-data replica serves its own WebSocket clients, so each reads the whole stream
(services/market-data-service/README.md). There is nothing to commit: the projection's stored
sequence per ticker says what it has applied, and anything it missed comes from the event store.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from market_data_service.runtime.events import EventEnvelope, InvalidEventError
from market_data_service.runtime.metrics import Metrics

logger = logging.getLogger("feed")

TOPIC = "matching.v1"


class MatchingFeed:
    def __init__(self, bootstrap_servers: str, service: str, metrics: Metrics) -> None:
        self._bootstrap = bootstrap_servers
        self._service = service
        self._metrics = metrics
        self._consumer: Any = None
        self._lag: int | None = None  # messages behind, summed; None until measured
        self.caught_up_once = False

    async def start(self) -> None:
        """Assign every partition and move to its end. Positions are resolved here, before the
        catch-up from the event store reads up to "now", so no event falls between the two."""
        from aiokafka import AIOKafkaConsumer, TopicPartition
        from aiokafka.client import AIOKafkaClient

        # Like runtime/checks.py: a short-lived client reads the cluster metadata.
        meta = AIOKafkaClient(bootstrap_servers=self._bootstrap, client_id=self._service)
        try:
            await meta.bootstrap()
            partitions = (await meta.fetch_all_metadata()).partitions_for_topic(TOPIC) or set()
        finally:
            await meta.close()
        if not partitions:
            raise RuntimeError(f"topic {TOPIC} has no partitions yet")
        consumer = AIOKafkaConsumer(
            bootstrap_servers=self._bootstrap,
            client_id=self._service,
            group_id=None,
            enable_auto_commit=False,
        )
        await consumer.start()
        try:
            assigned = [TopicPartition(TOPIC, p) for p in sorted(partitions)]
            consumer.assign(assigned)
            await consumer.seek_to_end(*assigned)
            for tp in assigned:
                await consumer.position(tp)
        except BaseException:
            await consumer.stop()
            raise
        self._consumer = consumer

    async def stop(self) -> None:
        if self._consumer is not None:
            await self._consumer.stop()
            self._consumer = None

    def caught_up(self) -> bool:
        return self._lag == 0

    async def run(self, handle: Callable[[EventEnvelope], Awaitable[None]]) -> None:
        """Hand every event to `handle`, in partition order. A failing `handle` (the event store
        or Cassandra is down) is retried with the same event until it succeeds."""
        while True:
            try:
                batches = await self._consumer.getmany(timeout_ms=500, max_records=200)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("feed poll failed, retrying in 1 s")
                self._lag = None
                await asyncio.sleep(1.0)
                continue
            for tp, records in batches.items():
                for record in records:
                    await self._deliver(handle, tp.topic, record.value)
            await self._update_lag()

    async def _deliver(
        self, handle: Callable[[EventEnvelope], Awaitable[None]], topic: str, value: bytes
    ) -> None:
        try:
            envelope = EventEnvelope.from_json(value)
        except InvalidEventError:
            logger.error("not an event envelope on matching.v1: skipped")
            self._metrics.event_processing_errors_total.labels(self._service, topic).inc()
            return
        delay = 0.2
        while True:
            try:
                await handle(envelope)
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._metrics.event_processing_errors_total.labels(self._service, topic).inc()
                logger.warning(
                    "applying an event failed, retrying",
                    extra={"event_id": envelope.event_id, "error": repr(exc)},
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5.0)
        self._metrics.events_consumed_total.labels(self._service, topic, envelope.event_type).inc()

    async def _update_lag(self) -> None:
        total = 0
        for tp in self._consumer.assignment():
            highwater = self._consumer.highwater(tp)
            if highwater is None:
                continue
            behind = max(highwater - await self._consumer.position(tp), 0)
            self._metrics.consumer_lag_messages.labels(
                self._service, tp.topic, str(tp.partition)
            ).set(behind)
            total += behind
        self._lag = total
        if total == 0:
            self.caught_up_once = True
