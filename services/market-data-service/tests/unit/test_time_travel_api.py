"""Q-12 over HTTP: answers, errors, and the places kept for the presenter (application/slots.py)."""

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from market_data_service.application.slots import Slots
from market_data_service.application.time_travel import TimeTravel
from market_data_service.config import Settings
from market_data_service.main import create_app
from market_data_service.ports.event_store import BookSnapshot, LiveBook
from tests.contract.test_response_schemas import check
from tests.fakes import Exchange, FakeEventStore, InMemoryMarketStore, ms

ADMIN = {"X-Role": "admin"}


def stamp(at_ms: int) -> str:
    moment = datetime.fromtimestamp(at_ms / 1000, UTC)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


TRADER = {"X-Role": "trader"}


@pytest.fixture
def exchange() -> Exchange:
    exchange = Exchange()
    exchange.place("a", "SELL", 20, 500)
    exchange.advance(1)
    exchange.place("b", "BUY", 5, 500)  # trades 5
    exchange.advance(1)
    exchange.place("b", "BUY", 3, 495)
    return exchange


class FakeLiveBooks:
    """matching-engine's Q-16: whatever the test says the live book is right now."""

    def __init__(self, exchange: Exchange) -> None:
        self.exchange = exchange
        self.book: LiveBook | None = None
        self.reads = 0

    def capture(self) -> LiveBook:
        e = self.exchange
        self.book = LiveBook(e.sequence, e.as_of or "", e.state_hash())
        return self.book

    async def live_book(self, ticker: str) -> LiveBook | None:
        self.reads += 1
        return self.book


@pytest.fixture
def live(exchange: Exchange) -> FakeLiveBooks:
    return FakeLiveBooks(exchange)


@pytest.fixture
def app(settings: Settings, exchange: Exchange, live: FakeLiveBooks) -> FastAPI:
    app = create_app(settings, readiness_checks=[])
    events = FakeEventStore()
    events.events["CALVERA"] = exchange.log
    now = ms(exchange.as_of or "") + 60_000
    latest = SimpleNamespace(as_of_ms=ms(exchange.as_of or ""))  # the live read model's last event
    app.state.projection = SimpleNamespace(
        ready=True, now_ms=lambda: now, markets={"CALVERA": latest}
    )
    app.state.time_travel = TimeTravel(events, InMemoryMarketStore(), live=live)
    return app


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def get(client: TestClient, at: str, headers: dict[str, str] | None = None) -> Any:
    return client.get("/v1/time-travel/CALVERA", params={"at": at}, headers=headers or TRADER)


def test_the_answer_is_the_book_at_t_with_its_hash(client: TestClient, exchange: Exchange) -> None:
    body = get(client, exchange.as_of or "").json()
    check("TimeTravelView", body)
    assert body["as_of_sequence"] == exchange.sequence
    assert body["state_hash"] == exchange.state_hash()
    assert body["order_book"]["bids"] == [{"price_paise": 495, "quantity": 3, "order_count": 1}]
    assert body["order_book"]["asks"] == [{"price_paise": 500, "quantity": 15, "order_count": 1}]
    earlier = get(client, exchange.log[0].occurred_at).json()  # after the first command only
    assert earlier["as_of_sequence"] == 1
    assert earlier["order_book"]["asks"][0]["quantity"] == 20


def test_rewinding_to_now_proves_the_rebuild_equals_the_live_engine_book(
    client: TestClient, exchange: Exchange, live: FakeLiveBooks
) -> None:
    captured = live.capture()
    body = get(client, exchange.as_of or "").json()
    check("TimeTravelView", body)
    assert body["verification"] == {
        "live_sequence": captured.sequence,
        "live_state_hash": captured.state_hash,
        "matches": True,
    }
    assert body["state_hash"] == captured.state_hash


def test_an_earlier_moment_is_not_compared_with_the_live_book(
    client: TestClient, exchange: Exchange, live: FakeLiveBooks
) -> None:
    live.capture()
    body = get(client, exchange.log[0].occurred_at).json()
    assert body["verification"] is None
    assert live.reads == 0  # the live engine is not asked about the past


def test_the_proof_holds_while_the_market_keeps_trading(
    client: TestClient, exchange: Exchange, live: FakeLiveBooks
) -> None:
    captured = live.capture()  # the engine's live book is read first ...
    exchange.place("c", "SELL", 2, 495)  # ... then bots trade in the same millisecond
    exchange.place("a", "BUY", 1, 500)
    body = get(client, exchange.as_of or "").json()
    assert body["as_of_sequence"] == captured.sequence < exchange.sequence
    assert body["verification"]["matches"] is True


def test_a_rebuild_that_differs_from_the_live_book_says_so(
    client: TestClient, exchange: Exchange, live: FakeLiveBooks
) -> None:
    captured = live.capture()
    live.book = LiveBook(captured.sequence, captured.as_of, "0" * 64)  # a different book
    body = get(client, exchange.as_of or "").json()
    assert body["verification"]["matches"] is False


def test_a_moment_in_the_future_or_without_an_offset_is_refused(
    client: TestClient, exchange: Exchange
) -> None:
    future = get(client, "2099-01-01T00:00:00Z")
    assert (future.status_code, future.json()["code"]) == (422, "TIME_IN_FUTURE")
    now = ms(exchange.as_of or "") + 60_000  # the fixture's server clock
    just_ahead = get(client, stamp(now + 1_500))  # a viewer's clock slightly ahead: counts as now
    assert (just_ahead.status_code, just_ahead.json()["at"]) == (200, stamp(now))
    assert get(client, stamp(now + 2_500)).json()["code"] == "TIME_IN_FUTURE"
    naive = get(client, "2026-11-20T10:42:00")
    assert (naive.status_code, naive.json()["code"]) == (422, "VALIDATION_FAILED")
    assert get(client, "yesterday").status_code == 422
    unknown = client.get("/v1/time-travel/NOPE", params={"at": exchange.as_of})
    assert unknown.status_code == 422


def test_a_full_replica_refuses_traders_but_keeps_a_place_for_the_presenter(
    client: TestClient, app: FastAPI, exchange: Exchange
) -> None:
    slots: Slots = app.state.rebuild_slots
    assert (slots.capacity, slots.reserved_for_admin) == (4, 1)
    for _ in range(3):  # three traders are time-travelling right now
        assert slots.try_acquire(admin=False)
    busy = get(client, exchange.as_of or "", TRADER)
    assert (busy.status_code, busy.json()["code"]) == (503, "OVERLOADED")
    assert get(client, exchange.as_of or "", ADMIN).status_code == 200  # the reserved place
    assert slots.in_use == 3  # every request gives its place back
    assert slots.try_acquire(admin=True)  # now even the presenter's place is taken
    full = get(client, exchange.as_of or "", ADMIN)
    assert (full.status_code, full.json()["code"]) == (503, "OVERLOADED")


def test_without_the_engine_the_answer_is_503(app: FastAPI, exchange: Exchange) -> None:
    class Down(FakeEventStore):
        async def snapshot(self, ticker: str, at_ms: int) -> BookSnapshot:
            raise httpx.ConnectError("matching-engine is down")

        async def events_after(
            self, ticker: str, after_sequence: int, *, until_ms: int | None = None
        ) -> AsyncIterator[Any]:
            raise httpx.ConnectError("matching-engine is down")
            yield  # pragma: no cover

    app.state.time_travel = TimeTravel(Down(), InMemoryMarketStore())
    with TestClient(app) as client:
        down = get(client, exchange.as_of or "")
    assert (down.status_code, down.json()["code"]) == (503, "DEPENDENCY_UNAVAILABLE")
    assert app.state.rebuild_slots.in_use == 0


def test_while_starting_the_answer_is_503(settings: Settings) -> None:
    with TestClient(create_app(settings, readiness_checks=[])) as client:
        starting = get(client, "2026-11-20T10:42:00Z")
    assert (starting.status_code, starting.json()["code"]) == (503, "DEPENDENCY_UNAVAILABLE")


def test_a_slot_count_that_leaves_no_place_for_everyone_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one"):
        Slots(1, 1)
    slots = Slots(2, 1)
    slots.release()  # releasing with nothing taken is harmless
    assert slots.in_use == 0
    assert slots.try_acquire(admin=False) and not slots.try_acquire(admin=False)
