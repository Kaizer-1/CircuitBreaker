"""Incident events to incidents.v1, keyed by incident_id (one incident's events stay in order).

Redpanda may not answer yet when the agent starts, and an incident never waits for it: events
are kept (up to 1,000) until the publisher has started, then sent in order. The store and the feed
have them meanwhile."""

import logging
from collections import deque

from sre_agent.runtime.events import EventEnvelope
from sre_agent.runtime.kafka import EventPublisher

logger = logging.getLogger("events")
TOPIC = "incidents.v1"
KEEP = 1000


class KafkaIncidentSink:
    def __init__(self, publisher: EventPublisher) -> None:
        self._publisher = publisher
        self._started = False
        self._waiting: deque[EventEnvelope] = deque(maxlen=KEEP)

    async def started(self) -> None:
        self._started = True
        while self._waiting:
            await self._send(self._waiting[0])
            self._waiting.popleft()

    async def publish(self, event: EventEnvelope) -> None:
        if not self._started or self._waiting:
            self._waiting.append(event)
            return
        await self._send(event)

    async def _send(self, event: EventEnvelope) -> None:
        await self._publisher.publish(TOPIC, str(event.data["incident_id"]), event)
