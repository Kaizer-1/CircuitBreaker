"""The HTTP event store (Q-14 paging, Q-15) and the live feed's delivery rules, no network."""

import json
from typing import Any

import httpx
import pytest

from market_data_service.adapters.engine_http import HttpEventStore
from market_data_service.adapters.matching_feed import MatchingFeed
from market_data_service.runtime.events import EventEnvelope, build_event
from market_data_service.runtime.metrics import Metrics


def event(sequence: int) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(
        build_event(
            "OrderCancelRejected",
            {"sequence": sequence, "ticker": "CALVERA"},
            producer="matching-engine",
            environment="dev",
            correlation_id="c",
        ).to_json()
    )
    return body


async def test_events_after_reads_every_page_in_order() -> None:
    seen: list[dict[str, str]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen.append(params)
        after = int(params["after_sequence"])
        page = [event(s) for s in range(after + 1, min(after + 3, 6))]
        last = page[-1]["data"]["sequence"] if page else None
        return httpx.Response(
            200,
            json={
                "ticker": "CALVERA",
                "events": page,
                "next_after_sequence": last,
                "has_more": bool(last and last < 5),
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
        store = HttpEventStore("http://engine/", client)
        got = [e.data["sequence"] async for e in store.events_after("CALVERA", 0)]
    assert got == [1, 2, 3, 4, 5]
    assert [p["after_sequence"] for p in seen] == ["0", "2", "4"]
    assert seen[0]["limit"] == "1000" and seen[0]["ticker"] == "CALVERA"


async def test_snapshot_reads_q15_at_a_time() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/internal/v1/snapshots/ASTRYX"
        assert request.url.params["at"].endswith("Z")
        return httpx.Response(
            200,
            json={
                "ticker": "ASTRYX",
                "sequence": 9,
                "as_of": "x",
                "bids": [],
                "asks": [{"price_paise": 5, "orders": []}],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
        snap = await HttpEventStore("http://engine", client).snapshot("ASTRYX", 1_795_000_000_000)
    assert (snap.sequence, snap.bids, len(snap.asks)) == (9, [], 1)


async def test_an_engine_error_is_raised_so_the_caller_retries() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(503))
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await HttpEventStore("http://engine", client).snapshot("ASTRYX", 0)


class _TP:
    def __init__(self, partition: int) -> None:
        self.topic, self.partition = "matching.v1", partition


class FakeConsumer:
    def __init__(self, behind: dict[int, int]) -> None:
        self.behind = behind

    def assignment(self) -> list[_TP]:
        return [_TP(p) for p in self.behind]

    def highwater(self, tp: _TP) -> int:
        return 100

    async def position(self, tp: _TP) -> int:
        return 100 - self.behind[tp.partition]


async def test_the_feed_retries_a_failing_handler_with_the_same_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("market_data_service.adapters.matching_feed.asyncio.sleep", no_sleep)
    feed = MatchingFeed("unused:9092", "market-data-service", Metrics("market-data-service"))
    calls: list[int] = []

    async def handle(envelope: EventEnvelope) -> None:
        calls.append(int(envelope.data["sequence"]))
        if len(calls) < 3:
            raise ConnectionError("matching-engine is down")

    await feed._deliver(handle, "matching.v1", json.dumps(event(7)).encode())
    await feed._deliver(handle, "matching.v1", b"not an envelope")  # skipped, logged
    assert calls == [7, 7, 7]


async def test_the_feed_is_caught_up_only_with_no_lag_on_any_partition() -> None:
    feed = MatchingFeed("unused:9092", "market-data-service", Metrics("market-data-service"))
    assert not feed.caught_up()  # not measured yet
    feed._consumer = FakeConsumer({0: 0, 1: 3})
    await feed._update_lag()
    assert not feed.caught_up() and not feed.caught_up_once
    feed._consumer = FakeConsumer({0: 0, 1: 0})
    await feed._update_lag()
    assert feed.caught_up() and feed.caught_up_once
