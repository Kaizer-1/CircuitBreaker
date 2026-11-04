"""Q-10 over a real WebSocket: identity only from X-Trader-Id, the portfolio first, then pushes."""

from datetime import UTC, datetime
from itertools import count
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from portfolio_service.application.portfolio import PortfolioService, seeded_trader_id
from portfolio_service.config import Settings
from portfolio_service.domain.model import AccountKind
from portfolio_service.main import create_app
from tests.builders import buy, sell, trade
from tests.fakes import InMemoryPortfolioStore

BOT03 = seeded_trader_id("bot-03")
URL = "/v1/ws/portfolio"


@pytest.fixture
def live(settings: Settings) -> Any:
    ids = count(1)
    store = InMemoryPortfolioStore()
    service = PortfolioService(
        store,
        clock=lambda: datetime(2026, 11, 20, 10, 42, tzinfo=UTC),
        new_id=lambda: f"3d2a1b0c-0000-4000-8000-{next(ids):012d}",
    )
    settings = settings.model_copy(update={"ws_ping_seconds": 0.05, "ws_pong_timeout_seconds": 0.3})
    app = create_app(settings, readiness_checks=[], service=service)
    store.listeners.append(app.state.hub.on_change)
    with TestClient(app) as client:
        assert client.portal is not None
        client.portal.call(service.seed_accounts)
        raju, _ = client.portal.call(service.open_account, "RajuTrader", AccountKind.HUMAN)
        yield client, service, raju.trader_id


def test_without_a_trader_identity_the_socket_is_closed_with_4401(live: Any) -> None:
    client, _, _ = live
    with client.websocket_connect(URL) as ws, pytest.raises(WebSocketDisconnect) as e:
        ws.receive_json()
    assert e.value.code == 4401


def test_an_unknown_trader_is_closed_with_4404(live: Any) -> None:
    client, _, _ = live
    headers = {"X-Trader-Id": "11111111-2222-4333-8444-555555555555"}
    with (
        client.websocket_connect(URL, headers=headers) as ws,
        pytest.raises(WebSocketDisconnect) as e,
    ):
        ws.receive_json()
    assert e.value.code == 4404


def test_the_portfolio_first_then_a_fill_and_the_updated_portfolio(live: Any) -> None:
    client, service, raju = live
    with client.websocket_connect(URL, headers={"X-Trader-Id": raju}) as ws:
        first = ws.receive_json()
        assert first["type"] == "portfolio" and first["data"]["cash_paise"] == 10_000_000
        client.portal.call(service.create_reservation, buy(1, raju, 20, 24600))
        client.portal.call(service.create_reservation, sell(2, BOT03, 20, 24550))
        client.portal.call(service.apply_event, trade(1, raju, 2, BOT03, 20, 24550, 0, 0))
        seen: list[dict[str, Any]] = []
        while (
            not seen or seen[-1]["type"] != "portfolio" or "fill" not in [m["type"] for m in seen]
        ):
            message = ws.receive_json()
            if message["type"] == "ping":
                ws.send_json({"type": "pong"})
                continue
            seen.append(message)
        kinds = [m["type"] for m in seen]
        assert kinds[-3:] == ["fill", "order_closed", "portfolio"]
        assert seen[-1]["data"]["cash_paise"] == 10_000_000 - 20 * 24550


def test_no_pong_closes_the_connection(live: Any) -> None:
    client, _, raju = live
    with client.websocket_connect(URL, headers={"X-Trader-Id": raju}) as ws:
        ws.receive_json()
        with pytest.raises(WebSocketDisconnect):
            while True:
                ws.receive_json()
