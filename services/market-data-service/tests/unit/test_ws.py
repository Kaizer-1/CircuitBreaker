"""Q-06: snapshot first, then updates in sequence; a client that disconnects and resyncs ends with
the same state as one that never disconnected, with no gaps and no duplicates.

`ClientView` applies the messages the way the web app does (web/src/market/marketState.ts):
the snapshot replaces everything; a trade at or below the sequence it already has is dropped;
a book replaces the book if it is newer; a candle replaces the one with the same bucket_start.
"""

import random
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from market_data_service.application.hub import QUEUE_LIMIT, MarketHub, Subscriber
from market_data_service.application.projection import MarketProjection
from market_data_service.config import Settings
from market_data_service.main import create_app
from tests.fakes import Clock, Exchange, FakeEventStore, InMemoryMarketStore, RecordingPublisher

TRADERS = ["a", "b", "c", "d"]


class ClientView:
    def __init__(self) -> None:
        self.book: dict[str, Any] | None = None
        self.trades: dict[int, dict[str, Any]] = {}
        self.candles: dict[str, dict[str, Any]] = {}
        self.duplicates = 0

    def apply(self, message: dict[str, Any]) -> None:
        kind: str = message["type"]
        data: Any = message.get("data")
        assert kind == "ping" or isinstance(data, dict)
        if kind == "snapshot":
            self.book = data["order_book"]
            self.trades = {t["sequence"]: t for t in data["recent_trades"]}
            self.candles = {c["bucket_start"]: c for c in data["candles"]}
        elif kind == "trade":
            assert self.book is not None
            if data["sequence"] <= self.book["as_of_sequence"] or data["sequence"] in self.trades:
                self.duplicates += 1
                return
            self.trades[data["sequence"]] = data
        elif kind == "book":
            assert self.book is not None
            if data["as_of_sequence"] >= self.book["as_of_sequence"]:
                self.book = data
        elif kind == "candle" and data["interval"] == "5s":
            self.candles[data["candle"]["bucket_start"]] = data["candle"]

    def drain(self, subscriber: Subscriber) -> None:
        while not subscriber.queue.empty():
            self.apply(subscriber.queue.get_nowait())

    def tape(self, n: int = 50) -> list[dict[str, Any]]:
        return [self.trades[s] for s in sorted(self.trades, reverse=True)[:n]]


class Market:
    def __init__(self) -> None:
        self.clock = Clock()
        self.exchange = Exchange(clock=self.clock)
        events = FakeEventStore()
        events.events["CALVERA"] = self.exchange.log
        self.projection = MarketProjection(
            InMemoryMarketStore(),
            events,
            RecordingPublisher(),
            environment="dev",
            clock=self.clock,
        )
        self.hub = MarketHub(self.projection)
        self.rng = random.Random(7)  # noqa: S311 (a repeatable simulation, not security)

    async def step(self) -> None:
        """One random order, delivered live, then the timed messages, then 2 s pass."""
        start = len(self.exchange.log)
        price = self.rng.randint(4900, 4920) * 5
        side = self.rng.choice(["BUY", "SELL"])
        self.exchange.place(self.rng.choice(TRADERS), side, self.rng.randint(1, 30), price)
        for event in self.exchange.log[start:]:
            await self.projection.handle(event)
        self.hub.flush_books()
        self.hub.send_forming_candles()
        self.clock.now += timedelta(seconds=2)
        await self.projection.tick()


async def test_a_client_that_reconnects_ends_with_the_same_state_as_one_that_never_left() -> None:
    m = Market()
    await m.projection.start()
    steady = ClientView()
    snapshot, s_sub = m.hub.subscribe("CALVERA")
    steady.apply(snapshot)

    flaky = ClientView()
    for _ in range(40):
        await m.step()
    snapshot, f_sub = m.hub.subscribe("CALVERA")
    flaky.apply(snapshot)
    for _ in range(40):
        await m.step()
    flaky.drain(f_sub)
    m.hub.unsubscribe(f_sub)  # the phone loses its connection; what is sent meanwhile is lost
    for _ in range(60):
        await m.step()
    snapshot, f_sub = m.hub.subscribe("CALVERA")  # reconnect: a fresh snapshot first
    flaky.apply(snapshot)
    for _ in range(40):
        await m.step()
    steady.drain(s_sub)
    flaky.drain(f_sub)

    market = m.projection.markets["CALVERA"]
    assert len(market.recent) > 50  # enough trades that the tape had to be resynced
    assert flaky.book == steady.book
    assert flaky.book is not None and flaky.book["as_of_sequence"] == market.sequence
    assert flaky.tape() == steady.tape()
    expected = [t.sequence for t in list(market.recent)[:50]]
    assert [t["sequence"] for t in flaky.tape()] == expected  # no gap, no duplicate
    assert flaky.duplicates == steady.duplicates == 0
    shared = sorted(set(flaky.candles) & set(steady.candles))
    assert len(shared) >= 60
    assert [flaky.candles[b] for b in shared] == [steady.candles[b] for b in shared]


async def test_the_snapshot_and_the_first_update_never_overlap_or_leave_a_gap() -> None:
    m = Market()
    await m.projection.start()
    for _ in range(10):
        await m.step()
    snapshot, sub = m.hub.subscribe("CALVERA")
    at = snapshot["data"]["order_book"]["as_of_sequence"]
    assert at == m.projection.markets["CALVERA"].sequence
    await m.step()
    trades = [msg["data"] for msg in _drain(sub) if msg["type"] == "trade"]
    assert all(t["sequence"] > at for t in trades)


async def test_a_client_too_far_behind_is_marked_for_closing() -> None:
    m = Market()
    await m.projection.start()
    _, sub = m.hub.subscribe("CALVERA")
    for _ in range(QUEUE_LIMIT + 1):
        sub.send({"type": "ping"})
    assert sub.lagging


def _drain(sub: Subscriber) -> list[dict[str, Any]]:
    out = []
    while not sub.queue.empty():
        out.append(sub.queue.get_nowait())
    return out


# ---------------------------------------------------------------------- over a real socket


@pytest.fixture
def live(settings: Settings) -> Any:
    settings = settings.model_copy(update={"ws_ping_seconds": 0.05, "ws_pong_timeout_seconds": 0.3})
    app = create_app(settings, readiness_checks=[])
    m = Market()
    with TestClient(app) as client:
        assert client.portal is not None
        client.portal.call(m.projection.start)
        app.state.projection, app.state.hub = m.projection, m.hub
        app.state.store = m.projection._store
        yield client, m


def test_an_unknown_ticker_is_closed_with_4404(live: Any) -> None:
    client, _ = live
    with (
        client.websocket_connect("/v1/ws/market/NOPE") as ws,
        pytest.raises(WebSocketDisconnect) as e,
    ):
        ws.receive_json()
    assert e.value.code == 4404


def test_while_starting_the_stream_closes_with_1011(settings: Settings) -> None:
    with TestClient(create_app(settings, readiness_checks=[])) as client:
        with (
            client.websocket_connect("/v1/ws/market/CALVERA") as ws,
            pytest.raises(WebSocketDisconnect) as e,
        ):
            ws.receive_json()
        assert e.value.code == 1011


def test_snapshot_first_then_trades_book_and_pings(live: Any) -> None:
    client, m = live
    with client.websocket_connect("/v1/ws/market/CALVERA") as ws:
        first = ws.receive_json()
        assert first["type"] == "snapshot"
        assert set(first["data"]) == {"order_book", "last_price_paise", "candles", "recent_trades"}
        m.exchange.place("a", "SELL", 5, 24600)
        m.exchange.place("b", "BUY", 5, 24600)
        for event in m.exchange.log:
            client.portal.call(m.projection.handle, event)
        client.portal.call(m.hub.flush_books)
        kinds = []
        while "book" not in kinds or "ping" not in kinds:
            message = ws.receive_json()
            kinds.append(message["type"])
            if message["type"] == "ping":
                ws.send_json({"type": "pong"})
        assert "trade" in kinds


def test_no_pong_closes_the_connection(live: Any) -> None:
    client, _ = live
    with client.websocket_connect("/v1/ws/market/CALVERA") as ws:
        ws.receive_json()  # snapshot
        with pytest.raises(WebSocketDisconnect):
            while True:
                ws.receive_json()  # pings, never answered


def test_subscribe_chooses_the_candle_intervals(live: Any) -> None:
    client, m = live
    with client.websocket_connect("/v1/ws/market/CALVERA") as ws:
        ws.receive_json()
        ws.send_json({"type": "subscribe", "data": {"intervals": ["1m", "nonsense"]}})
        ws.send_text("not json")  # ignored
        seen: set[str] = set()
        for _ in range(50):
            client.portal.call(m.hub.send_forming_candles)
            message = ws.receive_json()
            if message["type"] == "ping":
                ws.send_json({"type": "pong"})
            elif message["type"] == "candle":
                seen.add(message["data"]["interval"])
                if "1m" in seen:
                    break
        assert "1m" in seen  # (a 5s candle sent before the subscribe arrived may come first)
