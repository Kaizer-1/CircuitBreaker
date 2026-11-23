"""Q-13 over a WebSocket: checks, close codes, places kept for the presenter, abandoned sessions."""

import time
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from market_data_service.application.time_travel import TimeTravel
from market_data_service.config import Settings
from market_data_service.main import create_app
from tests.contract.test_response_schemas import check
from tests.fakes import Exchange, FakeEventStore, ms
from tests.unit.application.test_replay import market, store_for

ADMIN = {"X-Role": "admin"}
TRADER = {"X-Role": "trader"}


@pytest.fixture
def exchange() -> Exchange:
    return market(commands=12, gap_seconds=0.5)[0]


@pytest.fixture
def app(settings: Settings, exchange: Exchange) -> FastAPI:
    settings.replay_pause_limit_seconds = 0.3
    app = create_app(settings, readiness_checks=[])
    events = FakeEventStore()
    events.events["CALVERA"] = exchange.log
    now = ms(exchange.as_of or "") + 3_600_000
    app.state.projection = SimpleNamespace(ready=True, now_ms=lambda: now, markets={})
    app.state.time_travel = TimeTravel(events, store_for(exchange))
    return app


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def url(start: str, end: str, speed: Any = 60, ticker: str = "CALVERA") -> str:
    return f"/v1/ws/replay/{ticker}?from={start}&to={end}&speed={speed}"


def stamp(at_ms: int) -> str:
    return datetime.fromtimestamp(at_ms / 1000, UTC).isoformat().replace("+00:00", "Z")


def window(exchange: Exchange) -> tuple[str, str]:
    return exchange.log[0].occurred_at, stamp(ms(exchange.as_of or "") + 100)


def closed_with(client: TestClient, path: str, headers: dict[str, str] | None = None) -> int:
    with client.websocket_connect(path, headers=headers or TRADER) as ws:
        with pytest.raises(WebSocketDisconnect) as closed:
            while True:
                ws.receive_json()
        return closed.value.code


def test_a_replay_streams_frames_then_end_and_closes_normally(
    client: TestClient, exchange: Exchange
) -> None:
    start, end = window(exchange)
    messages: list[dict[str, Any]] = []
    with (
        client.websocket_connect(url(start, end), headers=TRADER) as ws,
        pytest.raises(WebSocketDisconnect) as closed,
    ):
        while True:
            messages.append(ws.receive_json())
    assert closed.value.code == 1000
    frames = [m["data"] for m in messages if m["type"] == "frame"]
    assert len(frames) == 12  # the book at `from` (after the first command), then 11 commands
    for frame in frames:
        check("OrderBookDepth", frame["order_book"])
        check("Candle", frame["candle"])
        if frame["last_trade"] is not None:
            check("Trade", frame["last_trade"])
        for trade in frame["trades"]:
            check("Trade", trade)
    end_message = messages[-1]
    assert end_message["type"] == "end"
    assert end_message["data"]["state_hash"] == exchange.state_hash()
    assert end_message["data"]["frames_sent"] == 12


@pytest.mark.parametrize(
    ("make_path", "code"),
    [
        (lambda s, e: url(s, e, ticker="NOPE"), 4404),
        (lambda s, e: url(s, e, speed=3), 4422),
        (lambda s, e: url(e, s), 4422),  # to before from
        (lambda s, e: url(s, "2099-01-01T00:00:00Z"), 4422),  # to in the future
        (lambda s, e: url(s, stamp(ms(s) + 31 * 60_000)), 4422),  # more than 30 minutes
        (  # from before the 24-hour horizon
            lambda s, e: url(stamp(ms(e) - 25 * 3_600_000), stamp(ms(e) - 24 * 3_600_000 - 60_000)),
            4422,
        ),
        (lambda s, e: url(s, e).replace("Z&", "&", 1), 4422),  # no UTC offset
        (lambda s, e: "/v1/ws/replay/CALVERA", 4422),  # nothing given
    ],
)
def test_bad_requests_are_closed_with_a_code_that_says_why(
    client: TestClient, exchange: Exchange, make_path: Any, code: int
) -> None:
    start, end = window(exchange)
    assert closed_with(client, make_path(start, end)) == code


def test_while_starting_the_replay_is_closed_with_1011(settings: Settings) -> None:
    with TestClient(create_app(settings, readiness_checks=[])) as client:
        code = closed_with(client, url("2026-11-20T10:00:00Z", "2026-11-20T10:01:00Z"))
    assert code == 1011


def test_a_full_replica_refuses_traders_with_1013_but_keeps_a_place_for_the_presenter(
    client: TestClient, app: FastAPI, exchange: Exchange
) -> None:
    for _ in range(3):
        assert app.state.replay_slots.try_acquire(admin=False)
    start, end = window(exchange)
    assert closed_with(client, url(start, end), TRADER) == 1013
    assert closed_with(client, url(start, end), ADMIN) == 1000  # played to the end
    assert app.state.replay_slots.in_use == 3


def test_an_abandoned_replay_gives_its_place_back_at_once(
    client: TestClient, app: FastAPI, exchange: Exchange
) -> None:
    start = exchange.log[0].occurred_at
    end = stamp(ms(start) + 20 * 60_000)  # 20 minutes at 1x: far longer than the test
    with client.websocket_connect(url(start, end, speed=1), headers=TRADER) as ws:
        assert ws.receive_json()["type"] == "frame"
        assert app.state.replay_slots.in_use == 1
    deadline = time.monotonic() + 2
    while app.state.replay_slots.in_use and time.monotonic() < deadline:
        time.sleep(0.01)
    assert app.state.replay_slots.in_use == 0


def active(client: TestClient, kind: str) -> float:
    for line in client.get("/metrics").text.splitlines():
        if line.startswith("time_travel_active{") and f'kind="{kind}"' in line:
            return float(line.rsplit(" ", 1)[1])
    raise AssertionError(f"no time_travel_active{{kind={kind}}} in /metrics")


def test_the_active_replays_gauge_counts_streams_while_they_run(
    client: TestClient, exchange: Exchange
) -> None:
    assert active(client, "replay") == 0
    start = exchange.log[0].occurred_at
    end = stamp(ms(start) + 20 * 60_000)
    with client.websocket_connect(url(start, end, speed=1), headers=TRADER) as ws:
        assert ws.receive_json()["type"] == "frame"
        assert active(client, "replay") == 1
        assert active(client, "rebuild") == 0
    deadline = time.monotonic() + 2
    while active(client, "replay") and time.monotonic() < deadline:
        time.sleep(0.01)
    assert active(client, "replay") == 0


def test_pause_resume_and_speed_messages_control_the_replay(
    client: TestClient, exchange: Exchange
) -> None:
    start = exchange.log[0].occurred_at
    end = stamp(ms(exchange.as_of or "") + 100)
    with client.websocket_connect(url(start, end, speed=1), headers=TRADER) as ws:
        assert ws.receive_json()["type"] == "frame"
        ws.send_json({"type": "speed", "data": {"speed": 7}})  # not an allowed speed: ignored
        ws.send_json({"type": "pause", "data": {}})
        ws.send_json({"type": "resume", "data": {}})
        ws.send_json({"type": "speed", "data": {"speed": 60}})
        began = time.monotonic()
        while (message := ws.receive_json())["type"] != "end":
            assert message["type"] == "frame"
        assert time.monotonic() - began < 2  # 5.5 s of market time at 60x, not 1x


def test_a_replay_left_paused_is_closed_and_frees_its_place(
    client: TestClient, app: FastAPI, exchange: Exchange
) -> None:
    start, end = window(exchange)
    with client.websocket_connect(url(start, end, speed=1), headers=TRADER) as ws:
        assert ws.receive_json()["type"] == "frame"
        ws.send_json({"type": "pause", "data": {}})
        with pytest.raises(WebSocketDisconnect) as closed:
            while True:
                ws.receive_json()
    assert closed.value.code == 1000
    assert app.state.replay_slots.in_use == 0
