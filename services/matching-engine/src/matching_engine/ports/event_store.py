"""The market event store, the market's source of truth (Cassandra keyspace matching_engine)."""

from collections.abc import Sequence
from typing import Any, Protocol

from matching_engine.runtime.events import EventEnvelope


class EventStore(Protocol):
    async def append(self, ticker: str, events: Sequence[EventEnvelope], command_id: str) -> None:
        """Store a command's events (consecutive sequences) and its command_id, all or nothing."""

    async def is_processed(self, command_id: str) -> bool: ...

    async def events_after(
        self,
        ticker: str,
        after_sequence: int,
        *,
        limit: int | None = None,
        until: str | None = None,
    ) -> list[EventEnvelope]:
        """Events with sequence > after_sequence (and occurred_at <= until), in sequence order."""

    async def latest_snapshot(self, ticker: str, at: str | None = None) -> dict[str, Any] | None:
        """The newest snapshot (taken at or before `at`, if given), as BookSnapshot.to_dict()."""

    async def save_snapshot(self, ticker: str, snapshot: dict[str, Any]) -> None: ...

    async def prune_snapshots(self, ticker: str, now: str) -> int:
        """Delete the snapshots domain/retention.py does not keep when the latest event is at `now`
        (never an event); returns how many partitions and rows were deleted."""

    async def publish_cursor(self, ticker: str) -> int:
        """The last sequence known to be published to matching.v1 (0 if none)."""

    async def set_publish_cursor(self, ticker: str, sequence: int) -> None: ...
