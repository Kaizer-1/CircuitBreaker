"""The internal read API (Q-14, Q-15, Q-16) against an engine on the in-memory store."""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from matching_engine.application.engine import MatchingEngine
from matching_engine.config import Settings
from matching_engine.main import create_app
from tests.builders import ALICE, BOB, id_source, limit
from tests.fakes import InMemoryEventStore, RecordingPublisher

NOW = datetime(2026, 11, 20, 10, 42, 1, 250_000, tzinfo=UTC)


@pytest.fixture
def engine() -> MatchingEngine:
    engine = MatchingEngine(
        InMemoryEventStore(),
        RecordingPublisher(),
        environment="dev",
        snapshot_every=2,
        clock=lambda: NOW,
        new_id=id_source(),
    )

    async def fill() -> None:
        await engine.recover()
        for command in (
            limit(1, BOB, "SELL", 20, 24550),
            limit(2, ALICE, "BUY", 50, 24600),  # trades 20, rests 30
            limit(3, BOB, "SELL", 5, 24700),
        ):
            await engine.handle(command)

    asyncio.run(fill())
    return engine


@pytest.fixture
def client(settings: Settings, engine: MatchingEngine) -> Iterator[TestClient]:
    with TestClient(create_app(settings, readiness_checks=[], engine=engine)) as c:
        yield c


def test_q16_returns_the_live_book_in_priority_order_with_its_hash(
    client: TestClient, engine: MatchingEngine
) -> None:
    body = client.get("/internal/v1/books/CALVERA").json()
    assert body["sequence"] == 4
    assert body["bids"] == [
        {
            "price_paise": 24600,
            "orders": [
                {
                    "order_id": "0dde0000-0000-4000-8000-000000000002",
                    "trader_id": ALICE,
                    "remaining_quantity": 30,
                    "accepted_sequence": 2,
                }
            ],
        }
    ]
    assert [lv["price_paise"] for lv in body["asks"]] == [24700]
    assert body["state_hash"] == engine.book("CALVERA").state_hash()


def test_q16_of_an_empty_book_is_sequence_0(client: TestClient) -> None:
    body = client.get("/internal/v1/books/TAVIRA").json()
    assert (body["sequence"], body["bids"], body["asks"]) == (0, [], [])
    assert body["as_of"].endswith("Z")


def test_q14_pages_through_events_in_sequence_order(client: TestClient) -> None:
    first = client.get("/internal/v1/events", params={"ticker": "CALVERA", "limit": 3}).json()
    assert [e["data"]["sequence"] for e in first["events"]] == [1, 2, 3]
    assert (first["next_after_sequence"], first["has_more"]) == (3, True)
    rest = client.get(
        "/internal/v1/events", params={"ticker": "CALVERA", "after_sequence": 3}
    ).json()
    assert [e["event_type"] for e in rest["events"]] == ["OrderAccepted"]
    assert (rest["next_after_sequence"], rest["has_more"]) == (4, False)
    empty = client.get("/internal/v1/events", params={"ticker": "KORVIN"}).json()
    assert (empty["events"], empty["next_after_sequence"], empty["has_more"]) == ([], None, False)


def test_q14_stops_at_until(client: TestClient) -> None:
    before = client.get(
        "/internal/v1/events", params={"ticker": "CALVERA", "until": "2026-11-20T10:00:00Z"}
    ).json()
    assert before["events"] == []


def test_q14_rejects_an_unknown_ticker_and_a_bad_limit(client: TestClient) -> None:
    assert client.get("/internal/v1/events", params={"ticker": "NOPE"}).status_code == 422
    bad = client.get("/internal/v1/events", params={"ticker": "CALVERA", "limit": 5000})
    assert bad.status_code == 422


def test_q15_returns_the_latest_snapshot_at_or_before_a_time(client: TestClient) -> None:
    body = client.get(
        "/internal/v1/snapshots/CALVERA", params={"at": "2026-11-20T11:00:00Z"}
    ).json()
    assert body["sequence"] == 3  # every 2 events: taken after the 2-event command (seq 2-3)
    assert body["bids"][0]["orders"][0]["remaining_quantity"] == 30
    # BOB's sell was filled by the trade at sequence 3: the snapshot says so, for the hash.
    assert body["closed"] == [
        {
            "order_id": "0dde0000-0000-4000-8000-000000000001",
            "state": "FILLED",
            "trader_id": BOB,
            "closed_at": body["as_of"],  # the trade was the snapshot's last event
        }
    ]
    assert body["forget_before"] == "2026-11-20T09:42:01.250Z"  # an hour before as_of
    assert body["as_of"] == "2026-11-20T10:42:01.250Z"


def test_q15_serves_a_snapshot_stored_before_closed_at_existed(
    client: TestClient, engine: MatchingEngine
) -> None:
    # Snapshots stored before 2026-11-14 have [order_id, state, trader_id] and no forget_before:
    # their closed orders count as closed at the snapshot's moment.
    stored = engine.store.snapshots["CALVERA"][-1]  # type: ignore[attr-defined]
    stored["closed"] = [c[:3] for c in stored["closed"]]
    del stored["forget_before"]
    body = client.get("/internal/v1/snapshots/CALVERA", params={"at": "2026-11-20T11:00:00Z"})
    assert body.json()["closed"][0]["closed_at"] == body.json()["as_of"]
    assert body.json()["forget_before"] is None


def test_q15_without_a_snapshot_yet_is_an_empty_book_at_sequence_0(client: TestClient) -> None:
    body = client.get("/internal/v1/snapshots/ASTRYX", params={"at": "2026-11-20T11:00:00Z"}).json()
    assert body == {
        "ticker": "ASTRYX",
        "sequence": 0,
        "as_of": "2026-11-20T11:00:00.000Z",
        "bids": [],
        "asks": [],
        "closed": [],
        "forget_before": None,
    }


def test_while_the_books_are_rebuilding_the_reads_say_so(settings: Settings) -> None:
    with TestClient(create_app(settings, readiness_checks=[])) as c:
        assert c.get("/internal/v1/books/CALVERA").status_code == 404
        events = c.get("/internal/v1/events", params={"ticker": "CALVERA"})
        assert (events.status_code, events.json()["code"]) == (503, "DEPENDENCY_UNAVAILABLE")
        snapshot = c.get("/internal/v1/snapshots/CALVERA", params={"at": "2026-11-20T11:00:00Z"})
        assert snapshot.status_code == 503
