"""Publishing to orders.v1 (key = ticker)."""

from typing import Protocol

from order_service.runtime.events import EventEnvelope


class OrderPublisher(Protocol):
    async def publish(self, topic: str, key: str, event: EventEnvelope) -> None: ...
