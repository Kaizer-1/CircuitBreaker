"""Helpers for the scripts that drive the running Compose stack (make demo-trade, make test-e2e).

Standard library only, so they run with the system python3. Everything goes through the public
gateway, except where a script must look at or break internals on purpose (it says so).
"""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

GATEWAY = os.environ.get("GATEWAY", "http://localhost:8080")
ENGINE = os.environ.get("ENGINE", "http://localhost:8002")  # internal API, dev host port only
PORTFOLIO = os.environ.get("PORTFOLIO", "http://localhost:8004")  # internal API, dev host port only
BOT_SECRET = os.environ.get("BOT_SECRET", "cb-dev-bot-secret")  # the Compose placeholder
ADMIN_PASSCODE = os.environ.get(
    "ADMIN_PASSCODE", "cb-dev-admin-passcode"
)  # the Compose placeholder
COMPOSE = ["docker", "compose", "--profile", "infra", "--profile", "app"]


# Pauses before each retry of a placement that got a transient 503 or timed out: 4 retries, 7.5 s.
PLACE_RETRY_PAUSES = (0.5, 1.0, 2.0, 4.0)


class ApiError(Exception):
    def __init__(self, status: int, body: Any) -> None:
        super().__init__(f"HTTP {status}: {body}")
        self.status, self.body = status, body


def http(method: str, url: str, body: Any = None, headers: dict[str, str] | None = None) -> Any:
    """One JSON call. A 429 (the gateway's rate limit) is waited out and retried, like a client."""
    data = json.dumps(body).encode() if body is not None else None
    for _ in range(10):
        request = urllib.request.Request(  # noqa: S310 (local http:// URLs from this file)
            url,
            method=method,
            data=data,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            if exc.code == 429:
                time.sleep(float(exc.headers.get("Retry-After", "1")))
                continue
            raise ApiError(exc.code, json.loads(raw) if raw else None) from None
    raise ApiError(429, "still rate limited after 10 tries")


class Trader:
    """A signed-in caller of the public API."""

    def __init__(self, session: dict[str, Any]) -> None:
        self.token = session["access_token"]
        self.trader_id: str = session["trader_id"]
        self.nickname: str = session["nickname"]
        self.retries = 0  # placements retried after a transient 503 or timeout (same key)

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def place(
        self,
        side: str,
        quantity: int,
        price: int | None,
        ticker: str = "CALVERA",
        *,
        key: str | None = None,
    ) -> dict[str, Any]:
        """A-05 like a real client: every placement carries an Idempotency-Key, and a transient
        503 or a timeout (a dependency briefly slow, e.g. portfolio-service on a 2-CPU CI runner)
        is retried with the SAME key, a bounded number of times with growing pauses. The server
        returns the first attempt's order instead of placing a second (api/order-service.yaml)."""
        order = {
            "ticker": ticker,
            "side": side,
            "order_type": "LIMIT" if price else "MARKET",
            "quantity": quantity,
        }
        if price is not None:
            order["limit_price_paise"] = price
        headers = {**self.auth, "Idempotency-Key": key or str(uuid.uuid4())}
        for pause in (*PLACE_RETRY_PAUSES, None):
            try:
                result: dict[str, Any] = http("POST", f"{GATEWAY}/v1/orders", order, headers)
                return result
            except (ApiError, TimeoutError, urllib.error.URLError) as exc:
                transient = not isinstance(exc, ApiError) or exc.status == 503
                if not transient or pause is None:
                    raise
                self.retries += 1
                time.sleep(pause)
        raise AssertionError("unreachable")

    def order(self, order_id: str) -> dict[str, Any]:
        result: dict[str, Any] = http("GET", f"{GATEWAY}/v1/orders/{order_id}", headers=self.auth)
        return result

    def orders(self, status: str | None = None) -> list[dict[str, Any]]:
        """All my orders (Q-07), page by page."""
        found: list[dict[str, Any]] = []
        cursor = None
        while True:
            query = (
                "limit=100"
                + (f"&status={status}" if status else "")
                + (f"&cursor={cursor}" if cursor else "")
            )
            page = http("GET", f"{GATEWAY}/v1/orders?{query}", headers=self.auth)
            found += page["items"]
            cursor = page["next_cursor"]
            if cursor is None:
                return found

    def cancel(self, order_id: str) -> dict[str, Any]:
        result: dict[str, Any] = http(
            "DELETE", f"{GATEWAY}/v1/orders/{order_id}", headers=self.auth
        )
        return result

    def portfolio(self) -> dict[str, Any]:
        result: dict[str, Any] = http("GET", f"{GATEWAY}/v1/portfolio", headers=self.auth)
        return result


def join(prefix: str) -> Trader:
    nickname = f"{prefix}{uuid.uuid4().hex[:6]}"[:20]
    return Trader(http("POST", f"{GATEWAY}/v1/sessions", {"nickname": nickname}))


def tester(n: int) -> Trader:
    """A trader for scripts and tests: load-test account `load-NN` (A-03, kind LOAD), never ranked
    on the leaderboard (Q-11), so test traders never reach the projector. load-01 ... load-10 are
    scripts/load_orders.py's; tests use 11 and up. These accounts carry over between runs (until
    make demo-reset), so checks compare with each one's own portfolio at the start."""
    if not 11 <= n <= 99:
        raise ValueError("test traders are load-11 ... load-99")
    return bot(f"load-{n:02d}")


def bot(name: str) -> Trader:
    return Trader(
        http(
            "POST",
            f"{GATEWAY}/v1/bot-sessions",
            {"account_name": name},
            {"X-Bot-Secret": BOT_SECRET},
        )
    )


def admin_token() -> str:
    """A presenter (role admin) session: A-04 with the dev passcode."""
    session = http(
        "POST", f"{GATEWAY}/v1/admin/sessions", {"nickname": "e2e", "passcode": ADMIN_PASSCODE}
    )
    return str(session["access_token"])


def wait_until_ready(timeout: float = 180) -> None:
    """Every service ready (stores reachable, consumers caught up), as the admin's status shows."""
    auth = {"Authorization": f"Bearer {admin_token()}"}

    def healthy() -> bool:
        return bool(http("GET", f"{GATEWAY}/v1/system/status", headers=auth)["status"] == "healthy")

    wait_for("every service to be ready", healthy, timeout=timeout, every=2)


def wait_for(what: str, check: Callable[[], Any], timeout: float = 15, every: float = 0.25) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() > deadline:
            raise TimeoutError(f"timed out after {timeout:.0f} s waiting for {what}")
        time.sleep(every)


def settled(
    trader: Trader, order_id: str, statuses: tuple[str, ...] = ("FILLED",)
) -> dict[str, Any]:
    result: dict[str, Any] = wait_for(
        f"order {order_id[:8]} to be {'/'.join(statuses)}",
        lambda: (o := trader.order(order_id))["status"] in statuses and o,
        timeout=30,
    )
    return result


def live_book(ticker: str = "CALVERA") -> dict[str, Any]:
    result: dict[str, Any] = http("GET", f"{ENGINE}/internal/v1/books/{ticker}")
    return result


def free_price(ticker: str, around: int) -> int:
    """A price inside the book's gap (above every bid, below every ask), nearest to `around`, so a
    test's own buy and sell meet each other and not orders left by someone else."""
    book = live_book(ticker)
    low = book["bids"][0]["price_paise"] + 5 if book["bids"] else 5
    high = book["asks"][0]["price_paise"] - 5 if book["asks"] else 10_000_000
    if low > high:
        raise RuntimeError(f"no free price in the {ticker} book")
    return min(max(around, low), high)


def engine_events(ticker: str = "CALVERA", after: int = 0) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    while True:
        page = http(
            "GET", f"{ENGINE}/internal/v1/events?ticker={ticker}&after_sequence={after}&limit=1000"
        )
        events += page["events"]
        if not page["has_more"]:
            return events
        after = page["next_after_sequence"]


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run([*COMPOSE, *args], check=check, capture_output=True, text=True)  # noqa: S603


def produce(topic: str, key: str, event: dict[str, Any]) -> None:
    """Write one message straight into a topic (rpk reads one record per line)."""
    subprocess.run(  # noqa: S603
        [*COMPOSE, "exec", "-T", "redpanda", "rpk", "topic", "produce", topic, "-k", key],
        input=json.dumps(event) + "\n",
        check=True,
        capture_output=True,
        text=True,
    )


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def rupees(paise: int) -> str:
    """Indian grouping: 1,00,00,000 paise -> ₹1,00,000.00."""
    sign, paise = ("-" if paise < 0 else ""), abs(paise)
    whole, fraction = divmod(paise, 100)
    digits = str(whole)
    head, tail = digits[:-3], digits[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return f"{sign}₹{','.join([*groups, tail]) if groups else tail}.{fraction:02d}"


class WebSocket:
    """Just enough of a WebSocket client (RFC 6455) for the scripts: JSON text messages, the
    protocol's ping/pong and close, and the channels' own {"type": "ping"} heartbeat (api/README).
    Standard library only, like the rest of this file."""

    def __init__(self, url: str, timeout: float = 60) -> None:
        parts = urllib.parse.urlsplit(url)
        self._sock = socket.create_connection((parts.hostname, parts.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        self._sock.sendall(
            (
                f"GET {target} HTTP/1.1\r\nHost: {parts.netloc}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        self._buffer = b""
        while b"\r\n\r\n" not in self._buffer:
            self._buffer += self._recv()
        head, self._buffer = self._buffer.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0].decode()
        if " 101 " not in status:
            raise ConnectionError(f"no WebSocket upgrade: {status}")
        self.close_code: int | None = None
        self.close_reason = ""

    def _recv(self) -> bytes:
        chunk = self._sock.recv(65536)
        if not chunk:
            raise ConnectionError("the connection dropped")
        return chunk

    def _read(self, count: int) -> bytes:
        while len(self._buffer) < count:
            self._buffer += self._recv()
        data, self._buffer = self._buffer[:count], self._buffer[count:]
        return data

    def _send(self, opcode: int, payload: bytes) -> None:
        mask, size = os.urandom(4), len(payload)  # a client always masks what it sends
        if size < 126:
            header = bytes([0x80 | opcode, 0x80 | size])
        elif size < 65536:
            header = bytes([0x80 | opcode, 0x80 | 126]) + size.to_bytes(2, "big")
        else:
            header = bytes([0x80 | opcode, 0x80 | 127]) + size.to_bytes(8, "big")
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(header + mask + masked)

    def send(self, message: dict[str, Any]) -> None:
        self._send(0x1, json.dumps(message).encode())

    def receive(self) -> dict[str, Any] | None:
        """The next message; None once the server has closed (then see close_code)."""
        message = b""
        while True:
            first, second = self._read(2)
            opcode, size = first & 0x0F, second & 0x7F
            if size == 126:
                size = int.from_bytes(self._read(2), "big")
            elif size == 127:
                size = int.from_bytes(self._read(8), "big")
            payload = self._read(size)
            if opcode == 0x8:  # close: answer it and stop
                self.close_code = int.from_bytes(payload[:2], "big") if payload else 1005
                self.close_reason = payload[2:].decode(errors="replace")
                try:
                    self._send(0x8, payload[:2])
                finally:
                    self._sock.close()
                return None
            if opcode == 0x9:
                self._send(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            message += payload
            if not first & 0x80:
                continue  # more fragments follow
            parsed: dict[str, Any] = json.loads(message)
            message = b""
            if parsed.get("type") == "ping":
                self.send({"type": "pong"})
                continue
            return parsed

    def close(self) -> None:
        try:
            self._send(0x8, (1000).to_bytes(2, "big"))
        finally:
            self._sock.close()


@dataclass
class Replay:
    frames: list[dict[str, Any]]
    end: dict[str, Any] | None
    close_code: int | None
    close_reason: str


def open_replay(token: str, ticker: str, start: str, until: str, speed: int) -> WebSocket:
    """Q-13 through the gateway, as the web app opens it."""
    query = urllib.parse.urlencode(
        {"from": start, "to": until, "speed": speed, "access_token": token}
    )
    return WebSocket(f"{GATEWAY.replace('http', 'ws', 1)}/v1/ws/replay/{ticker}?{query}")


def play(socket_: WebSocket) -> Replay:
    """Read a replay to its end: every frame, the end message and how it closed."""
    frames: list[dict[str, Any]] = []
    end = None
    while (message := socket_.receive()) is not None:
        if message["type"] == "frame":
            frames.append(message["data"])
        elif message["type"] == "end":
            end = message["data"]
    return Replay(frames, end, socket_.close_code, socket_.close_reason)
