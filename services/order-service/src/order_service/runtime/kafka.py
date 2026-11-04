"""Publishing and consuming events on Redpanda (Kafka API) with the event catalog's delivery rules.

- Producers wait for every replica to acknowledge (`acks=all`) and use the idempotent producer, so
  a retried send is not written twice and messages to one partition stay in order.
- Consumers commit an offset only after the handler has finished (at-least-once delivery), so
  handlers must be idempotent: the catalog's rule is to record each processed `event_id`.
- A message whose handler fails 5 times (or that is not a valid envelope) goes to `<topic>.dlq`
  with the error, and the consumer moves on. Infrastructure failures (a database or broker that is
  down) are retried until they recover instead: they say nothing about the message, and
  dead-lettering it would lose a valid event.

aiokafka is imported inside the methods, like the drivers in checks.py: not every service uses it.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from order_service.runtime.events import EventEnvelope, InvalidEventError, format_timestamp
from order_service.runtime.health import ReadinessCheck
from order_service.runtime.metrics import Metrics

logger = logging.getLogger("events")

MAX_ATTEMPTS = 5
# Errors that mean "the infrastructure is down", never "this message is bad". Services add the
# errors of their own drivers.
TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (OSError, TimeoutError)


class EventPublisher:
    """Publishes events (and dead letters) to Redpanda."""

    def __init__(self, bootstrap_servers: str, service: str, metrics: Metrics) -> None:
        self._bootstrap = bootstrap_servers
        self._service = service
        self._metrics = metrics
        self._producer: Any = None

    async def start(self) -> None:
        from aiokafka import AIOKafkaProducer

        self._producer = AIOKafkaProducer(
            bootstrap_servers=self._bootstrap,
            client_id=self._service,
            acks="all",
            enable_idempotence=True,
            request_timeout_ms=10_000,
        )
        await self._producer.start()

    async def stop(self) -> None:
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None

    async def publish(self, topic: str, key: str, event: EventEnvelope) -> None:
        await self.publish_raw(topic, key.encode(), event.to_json())
        self._metrics.events_published_total.labels(self._service, topic, event.event_type).inc()

    async def publish_raw(self, topic: str, key: bytes | None, value: bytes) -> None:
        if self._producer is None:
            raise RuntimeError("publisher not started")
        await self._producer.send_and_wait(topic, key=key, value=value)


@dataclass(frozen=True)
class Delivery:
    """Where a consumed message came from."""

    topic: str
    partition: int
    offset: int
    key: bytes | None
    value: bytes


Handler = Callable[[EventEnvelope, Delivery], Awaitable[None]]
DeadLetterSink = Callable[[str, bytes | None, bytes], Awaitable[None]]


class EventConsumer:
    """Runs `handler` for every message of `topics`, committing after each batch of a partition."""

    def __init__(
        self,
        *,
        bootstrap_servers: str,
        topics: Iterable[str],
        group_id: str,
        service: str,
        metrics: Metrics,
        handler: Handler,
        dead_letters: DeadLetterSink,
        transient_errors: tuple[type[BaseException], ...] = TRANSIENT_ERRORS,
        retry_delay_seconds: float = 0.2,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._bootstrap = bootstrap_servers
        self.topics = tuple(topics)
        self.group_id = group_id
        self._service = service
        self._metrics = metrics
        self._handler = handler
        self._dead_letters = dead_letters
        self._transient = transient_errors
        self._retry_delay = retry_delay_seconds
        self._sleep = sleep
        self._consumer: Any = None
        self._lag: dict[tuple[str, int], int] = {}
        self.caught_up_once = False  # every assigned partition reached its end at least once

    # ------------------------------------------------- one message (testable without Kafka)

    async def handle(self, delivery: Delivery) -> bool:
        """Process one message. True if handled, False if it went to the dead-letter topic."""
        try:
            event = EventEnvelope.from_json(delivery.value)
        except InvalidEventError as exc:
            await self._dead_letter(delivery, exc, attempts=1)
            return False

        attempts = 0
        backoff = self._retry_delay
        while True:
            try:
                await self._handler(event, delivery)
            except self._transient as exc:
                # The infrastructure is down: wait and retry, not counted against the message.
                self._count_error(delivery)
                logger.warning(
                    "transient error, retrying",
                    extra={"topic": delivery.topic, "event_id": event.event_id, "error": repr(exc)},
                )
                await self._sleep(backoff)
                backoff = min(backoff * 2, 5.0)
                continue
            except Exception as exc:
                attempts += 1
                self._count_error(delivery)
                if attempts >= MAX_ATTEMPTS:
                    await self._dead_letter(delivery, exc, attempts=attempts)
                    return False
                await self._sleep(self._retry_delay)
                continue
            self._metrics.events_consumed_total.labels(
                self._service, delivery.topic, event.event_type
            ).inc()
            return True

    def _count_error(self, delivery: Delivery) -> None:
        self._metrics.event_processing_errors_total.labels(self._service, delivery.topic).inc()

    async def _dead_letter(self, delivery: Delivery, error: Exception, *, attempts: int) -> None:
        body = {
            "original_topic": delivery.topic,
            "partition": delivery.partition,
            "offset": delivery.offset,
            "consumer_group": self.group_id,
            "attempts": attempts,
            "error": f"{type(error).__name__}: {str(error)[:500]}",
            "failed_at": format_timestamp(datetime.now(UTC)),
            "value": delivery.value.decode(errors="replace"),
        }
        logger.error(
            "message sent to the dead-letter topic",
            extra={"topic": delivery.topic, "offset": delivery.offset, "error": body["error"]},
        )
        await self._dead_letters(f"{delivery.topic}.dlq", delivery.key, json.dumps(body).encode())

    # ------------------------------------------------------------------ the Kafka loop

    async def start(self) -> None:
        from aiokafka import AIOKafkaConsumer

        self._consumer = AIOKafkaConsumer(
            *self.topics,
            bootstrap_servers=self._bootstrap,
            group_id=self.group_id,
            client_id=self._service,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
        )
        await self._consumer.start()

    async def stop(self) -> None:
        if self._consumer is not None:
            await self._consumer.stop()
            self._consumer = None

    async def run(self) -> None:
        """Consume until cancelled. Broker errors are logged and retried; it never gives up."""
        while True:
            try:
                batches = await self._consumer.getmany(timeout_ms=500, max_records=200)
                for tp, records in batches.items():
                    for r in records:
                        delivery = Delivery(tp.topic, tp.partition, r.offset, r.key, r.value)
                        await self.handle(delivery)
                    await self._consumer.commit({tp: records[-1].offset + 1})
                await self._update_lag()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("consumer loop error, retrying in 1 s")
                await self._sleep(1.0)

    async def _update_lag(self) -> None:
        lag: dict[tuple[str, int], int] = {}
        for tp in self._consumer.assignment():
            highwater = self._consumer.highwater(tp)
            if highwater is None:
                continue
            behind = max(highwater - await self._consumer.position(tp), 0)
            lag[(tp.topic, tp.partition)] = behind
            gauge = self._metrics.consumer_lag_messages
            gauge.labels(self._service, tp.topic, str(tp.partition)).set(behind)
        self._lag = lag
        if lag and all(v == 0 for v in lag.values()):
            self.caught_up_once = True

    def lag(self) -> dict[tuple[str, int], int]:
        """Messages behind, per assigned (topic, partition) whose end offset is known."""
        return dict(self._lag)

    def partitions_for(self, topic: str) -> set[int]:
        """Every partition of `topic` (from cluster metadata), whether assigned to us or not."""
        if self._consumer is None:
            return set()
        return set(self._consumer.partitions_for_topic(topic) or ())


def consumer_check(get_consumer: Callable[[], "EventConsumer | None"]) -> ReadinessCheck:
    """Ready only once the consumer has joined its group and read to the end of its partitions at
    least once (coding standards section 7: "consumers are caught up")."""

    async def probe() -> None:
        consumer = get_consumer()
        if consumer is None:
            raise RuntimeError("the consumer has not started yet")
        if not consumer.caught_up_once:
            raise RuntimeError(f"{consumer.group_id} has not caught up yet")

    return ReadinessCheck("consumer", probe)
