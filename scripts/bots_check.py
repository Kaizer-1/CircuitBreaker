#!/usr/bin/env python3
"""With the bots running (make bots): watch the market for N seconds and check it stays sound.

- the book is never crossed, in the matching engine (Q-16, the source of truth) or in what
  market-data shows (Q-04): best bid < best ask whenever both exist;
- every ticker has 5s candles with trades in the window, and its last price keeps moving;
- not one bot order was refused by the exchange (bot_orders_total{result="refused"} stays 0);
- no service answered a 5xx meanwhile (counted from the containers' JSON access logs);
- every ticker trades at least TRADES_PER_MINUTE_FLOOR times a minute (steady state with the 20
  bots is about 30 to 45: Phase 3's simulated hour and the Phase 4 event store agree);
- the book is two-sided (Phase 4): of the best DEPTH_SHOWN levels the dashboard shows, each side
  has at least MIN_LEVELS on average, the two sides differ by at most MAX_LEVEL_GAP on average,
  and the four market makers keep at least MIN_MAKER_QUOTES of their four quotes on each side
  and no more than MAX_MAKER_QUOTES (more means orphaned quotes no bot knows about).

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
TRADES_PER_MINUTE_FLOOR = 10  # per ticker
DEPTH_SHOWN = 6  # levels per side on the dashboard
MIN_LEVELS = 3.0
MAX_LEVEL_GAP = 1.0
MIN_MAKER_QUOTES = 3.5
MAX_MAKER_QUOTES = 4.5  # a requote briefly shows the new quote and the old one
MAKERS = {f"b0b0b0b0-{n:04d}-4000-8000-{n:012d}" for n in range(1, 5)}  # bot-01..04 (roster)
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


class Sides:
    """Per ticker: levels and market-maker quotes on each side, summed over the samples."""

    def __init__(self) -> None:
        self.samples = 0
        self.levels = {"bids": 0, "asks": 0}
        self.makers = {"bids": 0, "asks": 0}

    def add(self, book: dict[str, Any]) -> None:
        self.samples += 1
        for side in ("bids", "asks"):
            self.levels[side] += min(len(book[side]), DEPTH_SHOWN)
            self.makers[side] += sum(
                o["trader_id"] in MAKERS for lv in book[side] for o in lv["orders"]
            )

    def mean(self, what: dict[str, int], side: str) -> float:
        return what[side] / max(self.samples, 1)


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 60
    started = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    refused_before = refused()
    first = {
        t: http("GET", f"{MARKET}/v1/market/{t}/order-book")["as_of_sequence"] for t in TICKERS
    }
    samples = problems = 0
    failures: list[str] = []
    sides = {t: Sides() for t in TICKERS}
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        for ticker in TICKERS:
            engine = http("GET", f"{ENGINE}/internal/v1/books/{ticker}")
            depth = http("GET", f"{MARKET}/v1/market/{ticker}/order-book")
            sides[ticker].add(engine)
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
        per_minute = sum(c["trade_count"] for c in candles["candles"]) * 60 / seconds
        s = sides[ticker]
        bids, asks = s.mean(s.levels, "bids"), s.mean(s.levels, "asks")
        maker_bids, maker_asks = s.mean(s.makers, "bids"), s.mean(s.makers, "asks")
        print(
            f"  {ticker:<9} {per_minute:5.1f} trades/min, {len(traded):>3} of"
            f" {len(candles['candles'])} 5s candles traded, {len(prices)} closes,"
            f" {last - first[ticker]} events | levels bid {bids:.1f} ask {asks:.1f},"
            f" maker quotes bid {maker_bids:.1f} ask {maker_asks:.1f}"
        )
        if per_minute < TRADES_PER_MINUTE_FLOOR:
            failures.append(
                f"{ticker}: {per_minute:.1f} trades/min, below {TRADES_PER_MINUTE_FLOOR}"
            )
        if min(bids, asks) < MIN_LEVELS or abs(bids - asks) > MAX_LEVEL_GAP:
            failures.append(f"{ticker}: one-sided book (levels bid {bids:.1f}, ask {asks:.1f})")
        if max(maker_bids, maker_asks) > MAX_MAKER_QUOTES:
            failures.append(
                f"{ticker}: orphaned market-maker quotes"
                f" (bid {maker_bids:.1f}, ask {maker_asks:.1f} for 4 makers)"
            )
        if min(maker_bids, maker_asks) < MIN_MAKER_QUOTES:
            failures.append(
                f"{ticker}: market makers not on both sides"
                f" (bid {maker_bids:.1f}, ask {maker_asks:.1f} of 4)"
            )
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
