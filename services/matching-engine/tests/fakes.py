"""In-memory fakes for the engine's ports, with crash injection.

The store is "durable": it survives when a test throws the engine away (a crash) and builds a new
one. Every operation calls `hook(<operation>.<before|after>)` first, so a test can make the process
"die" at that exact point by raising Crash.
"""

from collections import defaultdict
from collections.abc import Callable, Sequence
from typing import Any

from matching_engine.domain.model import ms_of
from matching_engine.domain.retention import snapshots_to_keep
from matching_engine.runtime.events import EventEnvelope, parse_timestamp


class Crash(BaseException):  # noqa: N818 (a simulated process death, not an error to handle)
    """The process died. A BaseException, so no `except Exception` in the code catches it."""


def no_hook(point: str) -> None:
    return None


class InMemoryEventStore:
    def __init__(self) -> None:
        self.events: dict[str, list[EventEnvelope]] = defaultdict(list)
        self.processed: set[str] = set()
        self.snapshots: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.cursors: dict[str, int] = defaultdict(int)
        self.hook: Callable[[str], None] = no_hook
        self.reads = 0  # events returned by events_after, to check that snapshots bound recovery

    async def append(self, ticker: str, events: Sequence[EventEnvelope], command_id: str) -> None:
        self.hook("append.before")
        stored = self.events[ticker]
        for event in events:  # the real store refuses a sequence that is not the next one
            assert event.data["sequence"] == len(stored) + 1, "sequence gap in append"
            stored.append(event)
        self.processed.add(command_id)
        self.hook("append.after")

    async def is_processed(self, command_id: str) -> bool:
        return command_id in self.processed

    async def events_after(
        self,
        ticker: str,
        after_sequence: int,
        *,
        limit: int | None = None,
        until: str | None = None,
    ) -> list[EventEnvelope]:
        found = [e for e in self.events[ticker] if e.data["sequence"] > after_sequence]
        if until is not None:
            cut = parse_timestamp(until)
            found = [e for e in found if parse_timestamp(e.occurred_at) <= cut]
        if limit is not None:
            found = found[:limit]
        self.reads += len(found)
        return found

    async def latest_snapshot(self, ticker: str, at: str | None = None) -> dict[str, Any] | None:
        candidates = self.snapshots[ticker]
        if at is not None:
            cut = parse_timestamp(at)
            candidates = [s for s in candidates if parse_timestamp(s["as_of"]) <= cut]
        return max(candidates, key=lambda s: s["sequence"], default=None)

    async def save_snapshot(self, ticker: str, snapshot: dict[str, Any]) -> None:
        self.hook("snapshot.before")
        self.snapshots[ticker].append(snapshot)
        self.hook("snapshot.after")

    async def prune_snapshots(self, ticker: str, now: str) -> int:
        """The retention rule over the whole list (the real store applies it hour by hour)."""
        stored = self.snapshots[ticker]
        keep = snapshots_to_keep(((s["sequence"], ms_of(s["as_of"])) for s in stored), ms_of(now))
        self.snapshots[ticker] = [s for s in stored if s["sequence"] in keep]
        return len(stored) - len(self.snapshots[ticker])

    async def publish_cursor(self, ticker: str) -> int:
        return self.cursors[ticker]

    async def set_publish_cursor(self, ticker: str, sequence: int) -> None:
        self.hook("cursor.before")
        self.cursors[ticker] = sequence
        self.hook("cursor.after")


class RecordingPublisher:
    """Collects what was sent to matching.v1 (duplicates included, like a real topic)."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, EventEnvelope]] = []
        self.hook: Callable[[str], None] = no_hook
        self.calls: list[str] = []

    async def publish(self, ticker: str, event: EventEnvelope) -> None:
        self.hook("publish.before")
        self.sent.append((ticker, event))
        self.calls.append(f"publish {event.data['sequence']}")
        self.hook("publish.after")

    def first_copies(self, ticker: str) -> list[EventEnvelope]:
        """What a consumer that dedupes by event_id ends up with, in arrival order."""
        seen: set[str] = set()
        out = []
        for t, event in self.sent:
            if t == ticker and event.event_id not in seen:
                seen.add(event.event_id)
                out.append(event)
        return out
