"""Publishing CandleClosed to candles.v1 and dead letters (runtime EventPublisher)."""

from typing import Protocol

from market_data_service.runtime.events import EventEnvelope


class Publisher(Protocol):
    async def publish(self, topic: str, key: str, event: EventEnvelope) -> None: ...

    async def publish_raw(self, topic: str, key: bytes | None, value: bytes) -> None: ...
