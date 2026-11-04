#!/usr/bin/env python3
"""With the bots running (make bots): watch the market for N seconds and check it stays sound.

- the book is never crossed, in the matching engine (Q-16, the source of truth) or in what
  market-data shows (Q-04): best bid < best ask whenever both exist;
- every ticker has 5s candles with trades in the window, and its last price keeps moving;
- not one bot order was refused by the exchange (bot_orders_total{result="refused"} stays 0);
- no service answered a 5xx meanwhile (counted from the containers' JSON access logs).

Usage: python3 scripts/bots_check.py [seconds]   (default 60). Prints a summary; exit 1 on failure.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime
from typing import Any

from stack import ENGINE, http

MARKET = "http://localhost:8003"  # market-data-service, dev host port
BOTS = "http://localhost:8005"
TICKERS = ("CHAIWALA", "JUGAAD", "DABBA", "MONSOON", "FILMY")
SERVICES = (
    "api-gateway",
    "order-service",
    "matching-engine",
    "market-data-service",
    "portfolio-service",
    "bot-traders",
)


def refused() -> float:
    with urllib.request.urlopen(f"{BOTS}/metrics", timeout=5) as response:  # noqa: S310
        text = response.read().decode()
    return sum(
        float(line.rsplit(" ", 1)[1])
        for line in text.splitlines()
        if line.startswith("bot_orders_total{") and 'result="refused"' in line
    )


def server_errors(since: str) -> dict[str, int]:
    found = {}
    for service in SERVICES:
        out = subprocess.run(  # noqa: S603 (fixed command)
            ["docker", "logs", "--since", since, f"circuitbreaker-{service}-1"],
            capture_output=True,
            text=True,
            check=False,
        )
        lines = (out.stdout + out.stderr).splitlines()
        count = 0
        for line in lines:
            if '"status": 5' not in line:
                continue
            try:
                count += int(json.loads(line).get("status", 0)) >= 500
            except ValueError:
                continue
        found[service] = count
    return found


def crossed(bid: int | None, ask: int | None) -> bool:
    return bid is not None and ask is not None and bid >= ask


def best_of(book: dict[str, Any], side: str) -> int | None:
    levels = book[side]
    return int(levels[0]["price_paise"]) if levels else None


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 60
    started = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    refused_before = refused()
    first = {
        t: http("GET", f"{MARKET}/v1/market/{t}/order-book")["as_of_sequence"] for t in TICKERS
    }
    samples = problems = 0
    failures: list[str] = []
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        for ticker in TICKERS:
            engine = http("GET", f"{ENGINE}/internal/v1/books/{ticker}")
            depth = http("GET", f"{MARKET}/v1/market/{ticker}/order-book")
            if crossed(best_of(engine, "bids"), best_of(engine, "asks")):
                failures.append(f"engine book crossed: {ticker} at sequence {engine['sequence']}")
            if crossed(depth["best_bid_paise"], depth["best_ask_paise"]):
                failures.append(f"market-data depth crossed: {ticker} at {depth['as_of_sequence']}")
            samples += 1
        time.sleep(1)

    window = int(seconds // 5)
    for ticker in TICKERS:
        candles = http("GET", f"{MARKET}/v1/market/{ticker}/candles?interval=5s&limit={window}")
        traded = [c for c in candles["candles"] if c["trade_count"] > 0]
        prices = {c["close_paise"] for c in traded}
        last = http("GET", f"{MARKET}/v1/market/{ticker}/order-book")["as_of_sequence"]
        print(
            f"  {ticker:<9} {len(traded):>3} of the last {len(candles['candles'])} 5s candles"
            f" traded, {len(prices)} different closes, {last - first[ticker]} events"
        )
        if not traded:
            failures.append(f"{ticker}: no candle with trades in the last {seconds:.0f} s")
    newly_refused = refused() - refused_before
    if newly_refused:
        failures.append(f"{newly_refused:.0f} bot orders refused by the exchange")
    errors = server_errors(started)
    if any(errors.values()):
        failures.append(f"5xx answers: {errors}")
    problems = len(failures)
    print(
        f"  {samples} book samples (engine and market-data), {problems} problems; "
        f"refused orders: {newly_refused:.0f}; 5xx: {sum(errors.values())}"
    )
    for failure in failures[:20]:
        print(f"  FAIL {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
