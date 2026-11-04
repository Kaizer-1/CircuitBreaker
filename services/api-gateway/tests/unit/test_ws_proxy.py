"""The WebSocket proxy: token from ?access_token=, role per channel, identity headers built from the
token only, messages relayed both ways, upstream close codes passed through, token never logged.

The upstream is a real WebSocket server (the `websockets` library) on a free local port, running
in the app's own event loop, so the proxy is exercised end to end.
"""

import json
import logging
import socket
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from api_gateway.config import Settings
from api_gateway.domain.sessions import Role
from api_gateway.main import create_app

RAJU = "3d2a1b0c-9e8f-4a7b-8c6d-5e4f3a2b1c0d"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Upstream:
    """Says what it received (path, query, identity headers), echoes, and closes on request."""

    def __init__(self) -> None:
        self.server: Any = None
        self.port = free_port()

    async def start(self) -> None:
        import websockets

        async def handler(ws: Any) -> None:
            request = ws.request
            await ws.send(
                json.dumps(
                    {
                        "type": "hello",
                        "path": request.path,
                        "headers": {
                            k.lower(): v
                            for k, v in request.headers.raw_items()
                            if k.lower().startswith("x-")
                        },
                    }
                )
            )
            async for message in ws:
                body = json.loads(message)
                if body.get("type") == "close":
                    await ws.close(code=body["code"], reason="asked")
                    return
                await ws.send(json.dumps({"type": "echo", "data": body}))

        self.server = await websockets.serve(handler, "127.0.0.1", self.port)

    async def stop(self) -> None:
        self.server.close()
        await self.server.wait_closed()


@pytest.fixture
def gateway(settings: Settings) -> Iterator[tuple[TestClient, Any]]:
    upstream = Upstream()
    base = f"http://127.0.0.1:{upstream.port}"
    settings = settings.model_copy(
        update={
            "service_upstreams": {
                "market-data-service": base,
                "portfolio-service": base,
                "sre-agent": "http://127.0.0.1:9",  # nothing listens there
            }
        }
    )
    app = create_app(settings, readiness_checks=[])
    with TestClient(app) as client:
        assert client.portal is not None
        client.portal.call(upstream.start)
        yield client, app
        client.portal.call(upstream.stop)


def token(app: Any, role: Role = Role.TRADER, sub: str = RAJU, nickname: str = "RajuTrader") -> str:
    issued, _ = app.state.sessions.issue(sub, role, nickname)
    return str(issued)


def closed_with(client: TestClient, url: str, **kwargs: Any) -> int:
    with client.websocket_connect(url, **kwargs) as ws, pytest.raises(WebSocketDisconnect) as e:
        while True:
            ws.receive_json()
    return e.value.code


def test_without_a_valid_token_the_channel_closes_with_4401(gateway: Any) -> None:
    client, _ = gateway
    assert closed_with(client, "/v1/ws/market/CHAIWALA") == 4401
    assert closed_with(client, "/v1/ws/market/CHAIWALA?access_token=forged") == 4401


def test_the_portfolio_stream_is_for_traders_and_bots_only(gateway: Any) -> None:
    client, app = gateway
    admin = token(app, Role.ADMIN, "presenter", "presenter")
    assert closed_with(client, f"/v1/ws/portfolio?access_token={admin}") == 4403
    with client.websocket_connect(f"/v1/ws/portfolio?access_token={token(app, Role.BOT)}") as ws:
        assert ws.receive_json()["type"] == "hello"


def test_identity_comes_only_from_the_token_never_from_the_client(gateway: Any) -> None:
    client, app = gateway
    url = f"/v1/ws/portfolio?access_token={token(app)}"
    forged = {"X-Trader-Id": "b0b0b0b0-0003-4000-8000-000000000003", "X-Role": "admin"}
    with client.websocket_connect(url, headers=forged) as ws:
        hello = ws.receive_json()
    assert hello["path"] == "/v1/ws/portfolio"  # the token is not passed on
    headers = hello["headers"]
    assert (headers["x-trader-id"], headers["x-role"], headers["x-nickname"]) == (
        RAJU,
        "trader",
        "RajuTrader",
    )
    assert headers["x-correlation-id"]


def test_an_admin_watches_the_market_without_a_trader_id(gateway: Any) -> None:
    client, app = gateway
    admin = token(app, Role.ADMIN, "presenter", "presenter")
    with client.websocket_connect(f"/v1/ws/market/CHAIWALA?access_token={admin}") as ws:
        hello = ws.receive_json()
    assert "x-trader-id" not in hello["headers"] and hello["headers"]["x-role"] == "admin"


def test_other_query_parameters_are_passed_on(gateway: Any) -> None:
    client, app = gateway
    url = f"/v1/ws/replay/CHAIWALA?from=2026-11-20T10:00:00Z&speed=10&access_token={token(app)}"
    with client.websocket_connect(url) as ws:
        hello = ws.receive_json()
    assert hello["path"] == "/v1/ws/replay/CHAIWALA?from=2026-11-20T10%3A00%3A00Z&speed=10"


def test_messages_are_relayed_both_ways_and_close_codes_pass_through(gateway: Any) -> None:
    client, app = gateway
    with client.websocket_connect(f"/v1/ws/market/CHAIWALA?access_token={token(app)}") as ws:
        ws.receive_json()
        ws.send_json({"type": "pong"})
        assert ws.receive_json() == {"type": "echo", "data": {"type": "pong"}}
        ws.send_json({"type": "close", "code": 4404})
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
    assert e.value.code == 4404


def test_an_unreachable_upstream_closes_with_1011(gateway: Any) -> None:
    client, app = gateway
    assert closed_with(client, f"/v1/ws/incidents?access_token={token(app)}") == 1011


def test_the_access_token_never_appears_in_the_logs(
    gateway: Any, caplog: pytest.LogCaptureFixture
) -> None:
    client, app = gateway
    secret = token(app)
    caplog.set_level(logging.DEBUG)
    with client.websocket_connect(f"/v1/ws/market/CHAIWALA?access_token={secret}") as ws:
        ws.receive_json()
    closed_with(client, f"/v1/ws/incidents?access_token={secret}")  # logs a warning
    assert caplog.records
    assert all(
        secret not in r.getMessage() and secret not in str(r.__dict__) for r in caplog.records
    )


def test_uvicorns_websocket_log_line_is_redacted(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    # What uvicorn writes for each handshake (logger uvicorn.error), seen live in Compose.
    create_app(settings, readiness_checks=[])  # installs JSON logging and the filter
    logging.getLogger("uvicorn.error").info(
        '%s - "WebSocket %s" [accepted]',
        "172.20.0.6:57446",
        "/v1/ws/market/CHAIWALA?access_token=eyJhbGciOi.secret.part&x=1",
    )
    line = capsys.readouterr().out
    assert "eyJhbGciOi" not in line
    assert "access_token=[redacted]&x=1" in line
