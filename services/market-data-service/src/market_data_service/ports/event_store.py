"""matching-engine's event store, read through its internal API (Q-14, Q-15).

market-data never reads the engine's keyspace: the owner decides how its data is exposed.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

from market_data_service.runtime.events import EventEnvelope


@dataclass(frozen=True)
class BookSnapshot:
    sequence: int
    bids: list[dict[str, Any]]  # BookState price levels with their resting orders
    asks: list[dict[str, Any]]


class EventStore(Protocol):
    async def snapshot(self, ticker: str, at_ms: int) -> BookSnapshot:
        """Q-15: the latest snapshot at or before `at_ms` (sequence 0 and empty if none)."""
        ...

    def events_after(self, ticker: str, after_sequence: int) -> AsyncIterator[EventEnvelope]:
        """Q-14: every stored event after `after_sequence`, in sequence order, page by page."""
        ...
