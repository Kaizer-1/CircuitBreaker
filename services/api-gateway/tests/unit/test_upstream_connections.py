"""The gateway counts the TCP connections it opens to each upstream. A Kubernetes Service balances
connections, not requests, so this shows how often load can move to another pod (deployment doc)."""

import threading
import time

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from api_gateway.config import Settings
from api_gateway.main import create_app
from tests.unit.test_gateway import _with, bearer


def upstream() -> FastAPI:
    """order-service's Q-07, which asks to close the connection when told to (as a 503 OVERLOADED
    does)."""
    app = FastAPI()

    @app.get("/v1/orders")
    async def orders(close: int = 0) -> JSONResponse:
        headers = {"Connection": "close"} if close else {}
        return JSONResponse({"items": [], "next_cursor": None}, headers=headers)

    return app


class _RealServer:
    """An app behind a real uvicorn on a free local port: real TCP connections."""

    def __init__(self, app: FastAPI) -> None:
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_config=None)  # logging untouched
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> str:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


def opened(client: TestClient) -> float:
    prefix = 'gateway_upstream_connections_opened_total{upstream="order-service"} '
    lines = [line for line in client.get("/metrics").text.splitlines() if line.startswith(prefix)]
    assert lines, "the counter starts at 0 for every configured upstream"
    return float(lines[0].removeprefix(prefix))


def test_the_gateway_counts_each_connection_it_opens_to_an_upstream(settings: Settings) -> None:
    server = _RealServer(upstream())
    with (
        server as base,
        TestClient(create_app(_with(settings, **{"order-service": base}))) as client,
    ):
        assert opened(client) == 0
        for _ in range(3):
            assert client.get("/v1/orders", headers=bearer()).status_code == 200
        assert opened(client) == 1  # keep-alive: three requests on one connection
        for _ in range(2):
            assert client.get("/v1/orders?close=1", headers=bearer()).status_code == 200
        assert client.get("/v1/orders", headers=bearer()).status_code == 200
        # The first close ended the kept connection and the second its own, so the second and
        # the last request each needed a new one.
        assert opened(client) == 3
