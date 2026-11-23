"""The engine's command loop (docs/03-architecture/04-event-sourcing-and-cqrs.md, section 4).

For every command from orders.v1:

1. **Deduplicate:** a command_id already in the store is skipped (only the backlog is published).
2. **Decide:** the order book produces events; nothing changes yet.
3. **Append:** the events and the command_id are stored together (one logged batch).
4. **Apply:** the same events change the in-memory book (the function replay uses).
5. **Publish:** everything after the publish cursor goes to matching.v1, then the cursor moves.
6. The consumer commits the offset after `handle` returns (runtime/kafka.py).

A crash anywhere loses nothing: on restart `recover()` rebuilds every book from the latest snapshot
plus later events, and publishes whatever is after the cursor. A crash can only cause duplicates,
which consumers drop by event_id. One engine replica is the single writer of every book.
"""

import hashlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from matching_engine.domain.book import OrderBook
from matching_engine.domain.model import (
    TICKERS,
    BookSnapshot,
    Command,
    Decision,
    InvalidCommandError,
    ms_of,
)
from matching_engine.domain.retention import MIN_SPACING_MS
from matching_engine.events.commands import parse_command
from matching_engine.events.market import from_envelope, to_envelope
from matching_engine.ports.event_store import EventStore
from matching_engine.ports.publisher import MarketPublisher
from matching_engine.runtime.events import EventEnvelope, format_timestamp

logger = logging.getLogger("engine")

SNAPSHOT_EVERY = 500  # events per ticker (policy P-03), at most one per MIN_SPACING_MS


class MatchingEngine:
    def __init__(
        self,
        store: EventStore,
        publisher: MarketPublisher,
        *,
        environment: str,
        snapshot_every: int = SNAPSHOT_EVERY,
        snapshot_spacing_ms: int = MIN_SPACING_MS,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_id: Callable[[], str] = lambda: str(uuid4()),
        tickers: tuple[str, ...] = TICKERS,
    ) -> None:
        self._store = store
        self._publisher = publisher
        self._environment = environment
        self._snapshot_every = snapshot_every
        self._snapshot_spacing_ms = snapshot_spacing_ms
        self._clock = clock
        self._new_id = new_id
        self._books = {t: OrderBook(t) for t in tickers}
        self._unpublished: dict[str, list[EventEnvelope]] = {t: [] for t in tickers}
        self._last_snapshot: dict[str, int] = dict.fromkeys(tickers, 0)
        self._last_snapshot_at: dict[str, int | None] = dict.fromkeys(tickers)  # as_of, ms
        self.ready = False
        # Set when an append failed: the store may or may not hold the batch (a Cassandra write
        # timeout can still succeed later), so memory is not trusted until rebuilt from the store.
        self._stale = False

    def book(self, ticker: str) -> OrderBook:
        return self._books[ticker]

    @property
    def store(self) -> EventStore:
        """The event store, for the read API (Q-14, Q-15)."""
        return self._store

    def books_hash(self) -> str:
        """One hash over every book: equal hashes mean the whole market is identical."""
        combined = "|".join(f"{t}:{b.state_hash()}" for t, b in sorted(self._books.items()))
        return hashlib.sha256(combined.encode()).hexdigest()

    async def recover(self) -> None:
        """Rebuild every book from the event store alone, then publish what is not yet published."""
        for ticker in self._books:
            raw = await self._store.latest_snapshot(ticker)
            book = (
                OrderBook.from_snapshot(BookSnapshot.from_dict(raw)) if raw else OrderBook(ticker)
            )
            replayed = await self._store.events_after(ticker, book.sequence)
            for envelope in replayed:
                book.apply(from_envelope(envelope))
            self._books[ticker] = book
            self._last_snapshot[ticker] = raw["sequence"] if raw else 0
            self._last_snapshot_at[ticker] = ms_of(raw["as_of"]) if raw else None
            cursor = await self._store.publish_cursor(ticker)
            self._unpublished[ticker] = (
                await self._store.events_after(ticker, cursor) if cursor < book.sequence else []
            )
            logger.info(
                "book rebuilt",
                extra={
                    "ticker": ticker,
                    "sequence": book.sequence,
                    "from_snapshot": raw["sequence"] if raw else 0,
                    "replayed": len(replayed),
                    "to_publish": len(self._unpublished[ticker]),
                },
            )
        for ticker in self._books:
            await self._publish_pending(ticker)
        self.ready = True
        self._stale = False

    async def handle_envelope(self, envelope: EventEnvelope) -> Decision | None:
        command = parse_command(envelope)
        if command is None:
            logger.info("ignored unknown event type", extra={"event_type": envelope.event_type})
            return None
        return await self.handle(command)

    async def handle(self, command: Command) -> Decision | None:
        """Process one command. None means it was a duplicate (already processed)."""
        if not self.ready:
            raise RuntimeError("the engine must recover() before it handles commands")
        if self._stale:
            await self.recover()
        book = self._books.get(command.ticker)
        if book is None:
            raise InvalidCommandError(f"unknown ticker {command.ticker!r}")
        if await self._store.is_processed(command.command_id):
            await self._publish_pending(command.ticker)
            return None

        decision = book.decide(command, new_id=self._new_id, now=format_timestamp(self._clock()))
        envelopes = [to_envelope(e, self._environment) for e in decision.events]
        try:
            await self._store.append(command.ticker, envelopes, command.command_id)
        except Exception:
            self._stale = True
            raise
        for event in decision.events:
            book.apply(event)
        self._unpublished[command.ticker].extend(envelopes)
        if decision.detail:
            logger.info(
                "command not acted on",
                extra={
                    "outcome": decision.outcome,
                    "detail": decision.detail,
                    "ticker": book.ticker,
                },
            )
        await self._publish_pending(command.ticker)
        await self._maybe_snapshot(book)
        return decision

    async def _publish_pending(self, ticker: str) -> None:
        pending = self._unpublished[ticker]
        if not pending:
            return
        while pending:
            await self._publisher.publish(ticker, pending[0])
            last = pending.pop(0).data["sequence"]
        await self._store.set_publish_cursor(ticker, last)

    async def _maybe_snapshot(self, book: OrderBook) -> None:
        ticker = book.ticker
        if book.sequence - self._last_snapshot[ticker] < self._snapshot_every or book.as_of is None:
            return
        # At most one per 30 s of event time: under heavy trading 500 events come every few
        # seconds, and each snapshot is the whole book (megabytes with an hour of finished orders).
        last_at = self._last_snapshot_at[ticker]
        if last_at is not None and ms_of(book.as_of) - last_at < self._snapshot_spacing_ms:
            return
        await self._store.save_snapshot(ticker, book.snapshot().to_dict())
        self._last_snapshot[ticker] = book.sequence
        self._last_snapshot_at[ticker] = ms_of(book.as_of)
        logger.info("snapshot taken", extra={"ticker": ticker, "sequence": book.sequence})
        try:
            deleted = await self._store.prune_snapshots(ticker, book.as_of)
        except Exception:
            # Retention only saves space: the next snapshot prunes again (the rule is idempotent).
            logger.warning("snapshot pruning failed", extra={"ticker": ticker}, exc_info=True)
            return
        if deleted:
            logger.info("snapshots pruned", extra={"ticker": ticker, "deleted": deleted})
