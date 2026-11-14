"""matching-engine's event store and live books, read through its internal API (Q-14 to Q-16).

market-data never reads the engine's keyspace: the owner decides how its data is exposed.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from market_data_service.runtime.events import EventEnvelope


@dataclass(frozen=True)
class BookSnapshot:
    sequence: int
    bids: list[dict[str, Any]]  # BookState price levels with their resting orders
    asks: list[dict[str, Any]]
    as_of: str | None = None
    closed: list[dict[str, Any]] = field(default_factory=list)  # finished and expired orders
    forget_before: str | None = None  # the engine forgot the orders that closed before this


@dataclass(frozen=True)
class LiveBook:
    """The engine's live book right now (Q-16): its last event and its state hash."""

    sequence: int
    as_of: str
    state_hash: str


class EventStore(Protocol):
    async def snapshot(self, ticker: str, at_ms: int) -> BookSnapshot:
        """Q-15: the latest snapshot at or before `at_ms` (sequence 0 and empty if none)."""
        ...

    def events_after(
        self, ticker: str, after_sequence: int, *, until_ms: int | None = None
    ) -> AsyncIterator[EventEnvelope]:
        """Q-14: every stored event after `after_sequence` (and at or before `until_ms`, if
        given), in sequence order, page by page."""
        ...


class LiveBooks(Protocol):
    async def live_book(self, ticker: str) -> LiveBook | None:
        """Q-16: the engine's live book (None while the engine is still rebuilding it)."""
        ...
