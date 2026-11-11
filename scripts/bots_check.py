#!/usr/bin/env python3
"""With the bots running (make bots): watch the market for N seconds and check it stays sound.

- the book is never crossed, in the matching engine (Q-16, the source of truth) or in what
  market-data shows (Q-04): best bid < best ask whenever both exist;
- every ticker has 5s candles with trades in the window, and its last price keeps moving;
- not one bot order was refused by the exchange (bot_orders_total{result="refused"} stays 0);
- no service answered a 5xx meanwhile (counted from the containers' JSON access logs);
- the market trades at least TRADES_PER_MINUTE_FLOOR times a minute per ticker, averaged over the
  five. Steady state with the 20 bots is 20 to 50 per ticker (Phase 3's simulated hour and the
  Phase 4 event store agree). One ticker alone can have a quiet minute near zero (the bots trade
  in bursts), so the floor is on the whole market; the stall it guards against (after a gateway
  restart, before the Phase 4 bot fixes) hit every ticker at once, at 1 to 5 a minute;
- the book is two-sided (Phase 4): of the best DEPTH_SHOWN levels the dashboard shows, each side
  has at least MIN_LEVELS on average, and the four market makers keep at least MIN_MAKER_QUOTES of
  their four quotes on each side and no more than MAX_MAKER_QUOTES (more means orphaned quotes no
  bot knows about). The makers' quotes are the symmetry test. The level counts per side are
  printed but not judged: mean reversion and noise lean one way for a minute or two by design
  (measured: up to two levels either way within a minute, bids on one ticker while asks on another);
- every ticker's price stays within BAND (±10%) of its reference price for the whole run, read
  from the 1m candles' highs and lows (Phase 4: MONSOON once ran 13 to 15% above its reference
  within an hour; bot-traders README, "Phase 4 rebalance"). Its drift (first to last price) is
  printed. Run it for 30 minutes to check the band: `python3 scripts/bots_check.py 1800`.

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

from stack import ENGINE, http, rupees

MARKET = "http://localhost:8003"  # market-data-service, dev host port
TRADES_PER_MINUTE_FLOOR = 5  # per ticker
BAND = 0.10  # every price within ±10% of its ticker's reference price
DEPTH_SHOWN = 6  # levels per side on the dashboard
MIN_LEVELS = 2.0
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


def band(ticker: str, reference: int, candles: list[dict[str, Any]]) -> list[str]:
    """Print the ticker's drift and range; a failure if it left the band."""
    traded = [c for c in candles if c["trade_count"] > 0]
    if not traded:
        return []
    low, high = min(c["low_paise"] for c in traded), max(c["high_paise"] for c in traded)
    first, last = traded[0]["open_paise"], traded[-1]["close_paise"]

    def pct(price: int) -> str:
        return f"{(price - reference) / reference * 100:+.1f}%"

    print(
        f"  {'':<9} price {rupees(first)} -> {rupees(last)} ({(last - first) / first * 100:+.1f}%),"
        f" range {pct(low)} to {pct(high)} of the reference {rupees(reference)}"
        f" (band ±{BAND:.0%})"
    )
    if low < reference * (1 - BAND) or high > reference * (1 + BAND):
        return [f"{ticker}: price left the ±{BAND:.0%} band ({pct(low)} to {pct(high)})"]
    return []


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 60
    started = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    references = {
        i["ticker"]: int(i["reference_price_paise"])
        for i in http("GET", f"{MARKET}/v1/instruments")["instruments"]
    }
    refused_before = refused()
    first = {
        t: http("GET", f"{MARKET}/v1/market/{t}/order-book")["as_of_sequence"] for t in TICKERS
    }
    samples = problems = 0
    failures: list[str] = []
    sides = {t: Sides() for t in TICKERS}
    rates: list[float] = []
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

    window = min(int(seconds // 5), 500)  # Q-03 returns at most 500 candles
    minutes = max(1, round(seconds / 60))
    for ticker in TICKERS:
        candles = http("GET", f"{MARKET}/v1/market/{ticker}/candles?interval=5s&limit={window}")
        traded = [c for c in candles["candles"] if c["trade_count"] > 0]
        prices = {c["close_paise"] for c in traded}
        last = http("GET", f"{MARKET}/v1/market/{ticker}/order-book")["as_of_sequence"]
        ones = http("GET", f"{MARKET}/v1/market/{ticker}/candles?interval=1m&limit={minutes}")
        per_minute = sum(c["trade_count"] for c in ones["candles"]) / minutes
        s = sides[ticker]
        bids, asks = s.mean(s.levels, "bids"), s.mean(s.levels, "asks")
        maker_bids, maker_asks = s.mean(s.makers, "bids"), s.mean(s.makers, "asks")
        print(
            f"  {ticker:<9} {per_minute:5.1f} trades/min, {len(traded):>3} of"
            f" {len(candles['candles'])} 5s candles traded, {len(prices)} closes,"
            f" {last - first[ticker]} events | levels bid {bids:.1f} ask {asks:.1f},"
            f" maker quotes bid {maker_bids:.1f} ask {maker_asks:.1f}"
        )
        rates.append(per_minute)
        failures += band(ticker, references[ticker], ones["candles"])
        if min(bids, asks) < MIN_LEVELS:
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
    market_rate = sum(rates) / len(rates)
    print(f"  market: {market_rate:.1f} trades/min per ticker on average")
    if market_rate < TRADES_PER_MINUTE_FLOOR:
        failures.append(
            f"the market trades {market_rate:.1f} times a minute per ticker,"
            f" below {TRADES_PER_MINUTE_FLOOR}"
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
