"""Where incident events go: incidents.v1, keyed by incident_id (event catalog)."""

from typing import Protocol

from sre_agent.runtime.events import EventEnvelope


class EventSink(Protocol):
    async def publish(self, event: EventEnvelope) -> None: ...
