"""Publishing stored events to matching.v1 (key = ticker)."""

from typing import Protocol

from matching_engine.runtime.events import EventEnvelope


class MarketPublisher(Protocol):
    async def publish(self, ticker: str, event: EventEnvelope) -> None: ...
