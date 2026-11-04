"""The MarketPublisher port on Redpanda: topic matching.v1, key = ticker (event catalog)."""

from matching_engine.events.market import TOPIC
from matching_engine.runtime.events import EventEnvelope
from matching_engine.runtime.kafka import EventPublisher


class KafkaMarketPublisher:
    def __init__(self, publisher: EventPublisher) -> None:
        self._publisher = publisher

    async def publish(self, ticker: str, event: EventEnvelope) -> None:
        await self._publisher.publish(TOPIC, ticker, event)
