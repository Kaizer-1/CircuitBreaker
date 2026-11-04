"""The EventStore port over matching-engine's internal API (Q-14 events, Q-15 snapshots)."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx

from market_data_service.ports.event_store import BookSnapshot
from market_data_service.runtime.events import EventEnvelope, format_timestamp

PAGE = 1_000  # the largest page Q-14 allows


class HttpEventStore:
    def __init__(self, base_url: str, client: httpx.AsyncClient, *, timeout_seconds: float = 5.0):
        self._base = base_url.rstrip("/")
        self._client = client
        self._timeout = timeout_seconds

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        response = await self._client.get(
            f"{self._base}{path}", params=params, timeout=self._timeout
        )
        response.raise_for_status()
        return response.json()

    async def snapshot(self, ticker: str, at_ms: int) -> BookSnapshot:
        at = format_timestamp(datetime.fromtimestamp(at_ms / 1000, UTC))
        body = await self._get(f"/internal/v1/snapshots/{ticker}", {"at": at})
        return BookSnapshot(int(body["sequence"]), body["bids"], body["asks"])

    async def events_after(self, ticker: str, after_sequence: int) -> AsyncIterator[EventEnvelope]:
        after = after_sequence
        while True:
            page = await self._get(
                "/internal/v1/events", {"ticker": ticker, "after_sequence": after, "limit": PAGE}
            )
            for raw in page["events"]:
                yield EventEnvelope.model_validate(raw)
            if not page["has_more"] or page["next_after_sequence"] is None:
                return
            after = int(page["next_after_sequence"])
