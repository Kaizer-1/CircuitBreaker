"""The live feed (Q-19) served by real uvicorn, as in the container.

Starlette's TestClient speaks WebSocket without any library, so the API tests passed while the
image could not serve the feed at all: uvicorn needs a WebSocket library installed to accept an
upgrade, and without one it logs "No supported WebSocket library detected" and answers 404
(staging, 2026-11-27: the control room's feed never connected). Here a raw HTTP upgrade request
must get `101 Switching Protocols`, and the library must be a runtime dependency, not a dev one.
"""

import asyncio
import base64
import os
import socket
import tomllib
from pathlib import Path

import uvicorn

from sre_agent.main import create_app
from tests.fakes import Clock, World
from tests.unit.test_agent import Rig
from tests.unit.test_api import settings


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def test_real_uvicorn_accepts_the_feeds_websocket_upgrade() -> None:
    rig = Rig(World(Clock()))
    app = create_app(settings(), [], agent=rig.agent, store=rig.store)
    port = free_port()
    # log_config=None: uvicorn would otherwise replace the logging handlers of every other test.
    server = uvicorn.Server(uvicorn.Config(app, port=port, log_config=None, lifespan="on"))
    serving = asyncio.create_task(server.serve())
    try:
        for _ in range(250):  # up to 5 s for it to listen
            if server.started:
                break
            await asyncio.sleep(0.02)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        key = base64.b64encode(os.urandom(16)).decode()
        writer.write(
            (
                "GET /v1/ws/incidents HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        await writer.drain()
        status = await asyncio.wait_for(reader.readline(), timeout=5)
        writer.close()
        assert status.startswith(b"HTTP/1.1 101"), status
    finally:
        server.should_exit = True
        await serving


def test_the_websocket_library_is_a_runtime_dependency() -> None:
    project = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text())
    runtime = " ".join(project["project"]["dependencies"])
    assert "websockets" in runtime or "wsproto" in runtime
