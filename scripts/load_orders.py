#!/usr/bin/env python3
"""Order load through the public API: shows that more order-service replicas mean more capacity.

    python3 scripts/load_orders.py [--rate 200] [--seconds 120] [--mix orders|spike] [--json]

It signs in the load-test accounts load-01 ... load-10 (A-03, kind LOAD: never on the leaderboard),
the accounts chaos scenarios section 3.3 assigns to the traffic spike, and sends a steady rate of
requests through the gateway, spread over the accounts so each stays under the bot-token rate limit.
Every order is BUY 1 share, LIMIT 20% below the reference price: it rests and never trades.

    --mix orders (default)  50% POST /v1/orders, 50% DELETE /v1/orders/{id} (cancel the oldest),
                            so the accounts' ₹1,00,000 never runs out and it can run for any length
    --mix spike             the traffic spike's mix: 30% POST /v1/orders, 70% GET /v1/orders

Every 5 seconds it prints the rate it achieved and the latency (p50, p95) per kind of request as the
client saw it, plus any errors; at the end it cancels what is left. Phase 5's scale check
(scripts/k8s_checks.py scale) runs it at 2 order-service replicas, then at 4, and compares (Phase 7:
the agent does the scaling). Standard library only: one keep-alive connection per worker thread.
"""

from __future__ import annotations

import argparse
import collections
import http.client
import json
import os
import random
import signal
import sys
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from typing import Any

GATEWAY = os.environ.get("GATEWAY", "http://localhost")  # Kubernetes prod; Compose: :8080
BOT_SECRET = os.environ.get("BOT_SECRET", "cb-dev-bot-secret")  # the local placeholder
WINDOW = 5.0
# Set by SIGTERM (a check stopping the load early): the workers finish and the clean-up still runs,
# so no order is left resting with the account's cash reserved.
STOPPING = threading.Event()


@dataclass
class Account:
    name: str
    token: str
    resting: collections.deque[str] = field(default_factory=collections.deque)
    # Idempotency-Keys of placements that got no answer (timeout, 503 from the gateway): the order
    # may exist anyway. Placing again with the same key returns it, so the clean-up can cancel it.
    unanswered: list[dict[str, Any]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


class Stats:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.ms: dict[str, list[float]] = collections.defaultdict(list)
        self.codes: collections.Counter[str] = collections.Counter()

    def add(self, kind: str, ms: float, code: str) -> None:
        with self.lock:
            self.ms[kind].append(ms)
            if not code.startswith("2"):
                self.codes[f"{kind} {code}"] += 1

    def take(self) -> tuple[dict[str, list[float]], collections.Counter[str]]:
        with self.lock:
            out = (self.ms, self.codes)
            self.ms, self.codes = collections.defaultdict(list), collections.Counter()
            return out


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


class Client:
    """One keep-alive connection to the gateway (http.client: no new TCP handshake per request)."""

    def __init__(self) -> None:
        url = urllib.parse.urlsplit(GATEWAY)
        self.host, self.port = url.hostname or "localhost", url.port or 80
        self.conn = http.client.HTTPConnection(self.host, self.port, timeout=15)

    def call(
        self, method: str, path: str, token: str | None, body: Any = None, key: str | None = None
    ) -> tuple[int, Any]:
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Idempotency-Key"] = key
        if token:
            headers["Authorization"] = f"Bearer {token}"
        else:
            headers["X-Bot-Secret"] = BOT_SECRET  # the only unauthenticated call: A-03 bot sessions
        data = json.dumps(body) if body is not None else None
        for attempt in (1, 2):
            try:
                self.conn.request(method, path, body=data, headers=headers)
                response = self.conn.getresponse()
                raw = response.read()
                try:
                    return response.status, (json.loads(raw) if raw else None)
                except ValueError:  # a proxy's plain-text error page
                    return response.status, None
            except (http.client.HTTPException, OSError):
                self.conn.close()
                self.conn = http.client.HTTPConnection(self.host, self.port, timeout=15)
                if attempt == 2:
                    raise
        raise AssertionError("unreachable")


def sign_in(names: list[str]) -> list[Account]:
    client = Client()
    accounts = []
    for name in names:
        status, body = client.call("POST", "/v1/bot-sessions", None, {"account_name": name})
        if status != 201 and status != 200:
            sys.exit(f"sign-in of {name} failed: HTTP {status} {body}")
        accounts.append(Account(name, body["access_token"]))
    return accounts


def prices(token: str) -> dict[str, int]:
    """Per ticker: 20% below the reference price, on the 5-paise tick (a BUY that rests)."""
    _, body = Client().call("GET", "/v1/instruments", token)
    return {
        i["ticker"]: (int(i["reference_price_paise"] * 0.8) // i["tick_size_paise"])
        * i["tick_size_paise"]
        for i in body["instruments"]
    }


MIXES = {
    "orders": ("POST", "DELETE"),
    "spike": ("POST", "POST", "POST", "GET", "GET", "GET", "GET", "GET", "GET", "GET"),
}


def worker(
    mix: tuple[str, ...],
    accounts: list[Account],
    limits: dict[str, int],
    stats: Stats,
    next_slot: Any,
    deadline: float,
) -> None:
    client = Client()
    tickers = list(limits)
    while True:
        slot, at = next_slot()
        if at > deadline or STOPPING.is_set():
            return
        delay = at - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        account = accounts[slot % len(accounts)]
        kind = mix[(slot // len(accounts)) % len(mix)]
        oldest = None
        if kind == "DELETE":
            with account.lock:
                oldest = account.resting.popleft() if account.resting else None
            if oldest is None:  # nothing resting yet (slow answers): skip, never spend more cash
                continue
        started = time.monotonic()
        try:
            if kind == "POST":
                ticker = random.choice(tickers)  # noqa: S311 (load mix, not security)
                order = {
                    "ticker": ticker,
                    "side": "BUY",
                    "order_type": "LIMIT",
                    "quantity": 1,
                    "limit_price_paise": limits[ticker],
                }
                key = str(uuid.uuid4())
                try:
                    status, body = client.call("POST", "/v1/orders", account.token, order, key)
                except (http.client.HTTPException, OSError):
                    status, body = 0, None
                with account.lock:
                    if status == 202:
                        account.resting.append(body["order_id"])
                    elif status == 0 or status >= 500:
                        account.unanswered.append({"order": order, "key": key})
                if status == 0:
                    raise OSError("no answer")
            elif kind == "DELETE":
                status, _ = client.call("DELETE", f"/v1/orders/{oldest}", account.token)
                if status >= 500:  # try again in the clean-up
                    with account.lock:
                        account.resting.append(str(oldest))
            else:
                status, _ = client.call("GET", "/v1/orders", account.token)
            code = str(status)
        except (http.client.HTTPException, OSError) as exc:
            code = type(exc).__name__
        stats.add(kind, (time.monotonic() - started) * 1000, code)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--rate", type=float, default=200, help="requests per second (all kinds)")
    parser.add_argument("--mix", choices=sorted(MIXES), default="orders")
    parser.add_argument("--seconds", type=float, default=120)
    parser.add_argument("--accounts", type=int, default=10, help="how many load accounts")
    parser.add_argument("--first-account", type=int, default=1, help="load-NN to start from")
    # Enough workers that requests start on schedule even when responses are slow (open loop).
    parser.add_argument("--workers", type=int, default=300)
    parser.add_argument("--json", action="store_true", help="print a JSON summary at the end")
    args = parser.parse_args()

    first = args.first_account
    accounts = sign_in([f"load-{n:02d}" for n in range(first, first + args.accounts)])
    limits = prices(accounts[0].token)
    stats = Stats()
    start = time.monotonic() + 0.5
    deadline = start + args.seconds
    counter = iter(range(10**12))
    counter_lock = threading.Lock()

    def next_slot() -> tuple[int, float]:
        with counter_lock:
            slot = next(counter)
        return slot, start + slot / args.rate

    threads = [
        threading.Thread(
            target=worker,
            args=(MIXES[args.mix], accounts, limits, stats, next_slot, deadline),
            daemon=True,
        )
        for _ in range(args.workers)
    ]
    for t in threads:
        t.start()

    print(f"{args.rate:.0f} req/s ({args.mix}) for {args.seconds:.0f} s, {GATEWAY}")
    head = ["t (s)", "req/s", "POST p50", "POST p95", "DEL p95", "GET p95"]
    print(" ".join(f"{h:>9}" for h in head) + "  errors")
    windows = []
    signal.signal(signal.SIGTERM, lambda *_: STOPPING.set())
    while time.monotonic() < deadline + 1 and not STOPPING.is_set():
        time.sleep(WINDOW)
        ms, codes = stats.take()
        row = {
            "t": round(time.monotonic() - start),
            "rate": round(sum(len(v) for v in ms.values()) / WINDOW, 1),
            "post_p50_ms": round(percentile(ms["POST"], 0.5), 1),
            "post_p95_ms": round(percentile(ms["POST"], 0.95), 1),
            "delete_p95_ms": round(percentile(ms["DELETE"], 0.95), 1),
            "get_p95_ms": round(percentile(ms["GET"], 0.95), 1),
            "errors": dict(codes),
        }
        windows.append(row)
        errors = " ".join(f"{k}×{v}" for k, v in codes.items()) or "-"
        cells = [row["t"], row["rate"]] + [
            f"{row[k]}ms" for k in ("post_p50_ms", "post_p95_ms", "delete_p95_ms", "get_p95_ms")
        ]
        print(" ".join(f"{c:>9}" for c in cells) + f"  {errors}", flush=True)
    for t in threads:
        t.join(timeout=20)

    # Leave nothing resting, so the load accounts start clean next time: find the orders whose
    # placement got no answer (same Idempotency-Key = the same order, if it was created), then
    # cancel every order still resting.
    client = Client()
    left = found = 0
    for account in accounts:
        for attempt in account.unanswered:
            status, body = client.call(
                "POST", "/v1/orders", account.token, attempt["order"], attempt["key"]
            )
            if status in (200, 201, 202) and body and "order_id" in body:
                account.resting.append(body["order_id"])
                found += 1
        while account.resting:
            client.call("DELETE", f"/v1/orders/{account.resting.popleft()}", account.token)
            left += 1
    print(f"cancelled the {left} orders still resting ({found} found again by Idempotency-Key)")
    if args.json:
        steady = windows[2:-1] or windows
        print(
            json.dumps(
                {
                    "windows": windows,
                    "post_p95_ms_median": sorted(w["post_p95_ms"] for w in steady)[
                        len(steady) // 2
                    ],
                    "rate_median": sorted(w["rate"] for w in steady)[len(steady) // 2],
                }
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
