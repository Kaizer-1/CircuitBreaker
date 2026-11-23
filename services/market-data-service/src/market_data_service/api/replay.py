"""Q-13, the replay WebSocket `/v1/ws/replay/{ticker}?from=&to=&speed=` (api/README.md).

Checks the request, takes a replay place (application/slots.py), then runs three tasks side by
side: the replay itself, the viewer's messages (`pause`, `resume`, `speed`, `pong`) and the
heartbeat. Whichever ends first ends the others, and the place is always given back, so a viewer
who closes the tab or loses Wi-Fi frees it at once.

Close codes: 4404 unknown ticker, 4422 invalid parameters (the reason says which), 1013 this
replica is already running its replays (try again later), 1011 market data is starting or the
event store stopped answering, 1000 the replay ended or stayed paused too long.
"""

import asyncio
import contextlib
import json
import logging
import time
from typing import Any

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from market_data_service.api.time_travel import at_ms, is_admin
from market_data_service.application.replay import (
    MAX_WINDOW_MS,
    SPEEDS,
    PausedTooLongError,
    ReplaySession,
)
from market_data_service.application.slots import Slots
from market_data_service.application.time_travel import HORIZON_MS
from market_data_service.domain.instruments import TICKERS

logger = logging.getLogger("replay")

router = APIRouter()


def _window(params: Any, now_ms: int) -> tuple[int, int, int]:
    """(from, to, speed) or ValueError with a reason a person can act on."""
    try:
        start, end = at_ms(params["from"]), at_ms(params["to"])
        speed = int(params["speed"])
    except (KeyError, ValueError):
        raise ValueError("from, to (ISO-8601 with an offset) and speed are required") from None
    if speed not in SPEEDS:
        raise ValueError(f"speed must be one of {', '.join(map(str, SPEEDS))}")
    if end <= start:
        raise ValueError("to must be after from")
    if end - start > MAX_WINDOW_MS:
        raise ValueError("the window is at most 30 minutes")
    if end > now_ms:
        raise ValueError("to is in the future")
    if start < now_ms - HORIZON_MS:
        raise ValueError("from is more than 24 hours ago (time travel reaches back 24 hours)")
    return start, end, speed


@router.websocket("/v1/ws/replay/{ticker}")
async def stream_replay(websocket: WebSocket, ticker: str) -> None:
    await websocket.accept()
    app = websocket.app
    if ticker not in TICKERS:
        await websocket.close(code=4404, reason="unknown ticker")
        return
    projection, time_travel = app.state.projection, app.state.time_travel
    if projection is None or not projection.ready or time_travel is None:
        await websocket.close(code=1011, reason="market data is starting")
        return
    try:
        start, end, speed = _window(websocket.query_params, projection.now_ms())
    except ValueError as exc:
        await websocket.close(code=4422, reason=str(exc))
        return
    slots: Slots = app.state.replay_slots
    if not slots.try_acquire(admin=is_admin(websocket.headers.get("x-role"))):
        await websocket.close(code=1013, reason="too many replays on this replica: try again")
        return
    settings = app.state.settings
    try:
        session = ReplaySession(
            time_travel,
            ticker=ticker,
            from_ms=start,
            to_ms=end,
            speed=speed,
            pause_limit_seconds=settings.replay_pause_limit_seconds,
        )
        await _serve(websocket, session, settings.ws_ping_seconds, settings.ws_pong_timeout_seconds)
    finally:
        slots.release()


async def _serve(
    websocket: WebSocket, session: ReplaySession, ping_every: float, pong_timeout: float
) -> None:
    last_pong = time.monotonic()

    async def replay() -> tuple[int, str]:
        try:
            await session.run(websocket.send_json)
        except PausedTooLongError:
            return 1000, "paused for too long"
        except httpx.HTTPError:
            return 1011, "the event store stopped answering"
        return 1000, "replay ended"

    async def receive() -> None:
        nonlocal last_pong
        while True:
            try:
                message: Any = json.loads(await websocket.receive_text())
            except ValueError:
                continue  # not JSON: ignored, like an unknown message type
            kind = message.get("type") if isinstance(message, dict) else None
            data = message.get("data") if isinstance(message, dict) else None
            if kind == "pong":
                last_pong = time.monotonic()
            elif kind == "pause":
                session.pacer.pause()
            elif kind == "resume":
                session.pacer.resume()
            elif kind == "speed" and isinstance(data, dict) and data.get("speed") in SPEEDS:
                session.pacer.set_speed(int(data["speed"]))

    async def ping() -> tuple[int, str]:
        while True:
            await asyncio.sleep(ping_every)
            if time.monotonic() - last_pong > pong_timeout:
                return 1000, "no pong"
            await websocket.send_json({"type": "ping"})

    tasks = [asyncio.create_task(t()) for t in (replay, receive, ping)]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            exc = task.exception()
            if exc is not None:
                if not isinstance(exc, WebSocketDisconnect):
                    logger.warning("replay ended", extra={"error": repr(exc)})
                continue
            result = task.result()
            if result is not None:
                code, reason = result
                with contextlib.suppress(Exception):
                    await websocket.close(code=code, reason=reason)
    finally:
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
