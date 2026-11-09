"""The EventStore and LiveBooks ports over matching-engine's internal API (Q-14 events, Q-15
snapshots, Q-16 the live book and its hash)."""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx

from market_data_service.ports.event_store import BookSnapshot, LiveBook
from market_data_service.runtime.events import EventEnvelope, format_timestamp

PAGE = 1_000  # the largest page Q-14 allows
PARSE_IN_THREAD_BYTES = 64_000  # a snapshot with thousands of finished orders: off the event loop


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
        if len(response.content) > PARSE_IN_THREAD_BYTES:
            return await asyncio.to_thread(json.loads, response.content)
        return response.json()

    async def snapshot(self, ticker: str, at_ms: int) -> BookSnapshot:
        body = await self._get(f"/internal/v1/snapshots/{ticker}", {"at": _iso(at_ms)})
        return BookSnapshot(
            int(body["sequence"]),
            body["bids"],
            body["asks"],
            as_of=body.get("as_of"),
            closed=body.get("closed", []),
        )

    async def live_book(self, ticker: str) -> LiveBook | None:
        response = await self._client.get(
            f"{self._base}/internal/v1/books/{ticker}", timeout=self._timeout
        )
        if response.status_code == 404:  # still rebuilding its books
            return None
        response.raise_for_status()
        body = response.json()
        return LiveBook(int(body["sequence"]), body["as_of"], body["state_hash"])

    async def events_after(
        self, ticker: str, after_sequence: int, *, until_ms: int | None = None
    ) -> AsyncIterator[EventEnvelope]:
        after = after_sequence
        params: dict[str, Any] = {"ticker": ticker, "limit": PAGE}
        if until_ms is not None:
            params["until"] = _iso(until_ms)
        while True:
            page = await self._get("/internal/v1/events", {**params, "after_sequence": after})
            for raw in page["events"]:
                yield EventEnvelope.model_validate(raw)
            if not page["has_more"] or page["next_after_sequence"] is None:
                return
            after = int(page["next_after_sequence"])


def _iso(at_ms: int) -> str:
    return format_timestamp(datetime.fromtimestamp(at_ms / 1000, UTC))
