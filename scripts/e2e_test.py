#!/usr/bin/env python3
"""make test-e2e: the Phase 2 "done when" checks against the running Compose stack (make up first).

1. place -> match -> settle through the gateway, with exact balances. Every trader a script
   makes is a load-test account (stack.tester): never on the leaderboard.
2. Crash recovery: the matching engine is killed (SIGKILL) in the middle of a workload and
   restarted; afterwards the rebuilt book has the same hash as before an idle kill, every order
   agrees with the engine's trades, and cash and shares are conserved.
3. The orphan rule, fail-closed (ADR-0011, ADR-0017), with Redpanda stopped: an order that cannot
   be published is REJECTED within 60 s; its money, and the money of a reservation whose order was
   never stored, stays reserved while the engine cannot answer, and is released only after it
   does; a late copy of an expired order is refused.
4. Phase 3: the 20 bots trade for 60 s (make bots): candles for all 5 tickers, the book never
   crossed, no refused bot order, no 5xx, the trade-rate floor and a two-sided book.
5. Phase 4: no trader a test made is ever ranked on the leaderboard (Q-11), and A-01 refuses a
   nickname on the blocklist without opening an account.

Takes about 7 minutes (the orphan rule waits for real 60 s and 120 s timers; the bots run 60 s).
"""

from __future__ import annotations

import random
import subprocess
import sys
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from stack import (
    ENGINE,
    GATEWAY,
    PORTFOLIO,
    ApiError,
    Trader,
    bot,
    compose,
    engine_events,
    free_price,
    http,
    live_book,
    now_iso,
    produce,
    settled,
    tester,
    wait_for,
    wait_until_ready,
)

FINAL = ("FILLED", "CANCELLED", "REJECTED")


def parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def engine_ready() -> bool:
    try:
        return http("GET", f"{ENGINE}/readyz")["status"] == "ready"
    except Exception:
        return False


def restart_engine(signal: str = "SIGKILL") -> None:
    compose("kill", "-s", signal, "matching-engine")
    compose("up", "-d", "matching-engine")
    wait_for("the matching engine to rebuild its books", engine_ready, timeout=120, every=1)
    wait_until_ready()


# ------------------------------------------------------------------ 1


def test_place_trade_settle() -> None:
    a, b, seller = tester(11), tester(12), bot("bot-01")
    cancel_everything([a, b])  # orders left by an earlier, interrupted run
    a0, b0 = a.portfolio(), b.portfolio()
    p = free_price("CHAIWALA", 24550)  # nobody else's resting order can take part
    sell = seller.place("SELL", 20, p)
    buy = a.place("BUY", 20, p + 5)  # willing to pay 5 paise more: pays the resting price
    assert buy["status"] == "PENDING"
    settled(seller, sell["order_id"])
    filled = settled(a, buy["order_id"])
    assert (filled["filled_quantity"], filled["average_fill_price_paise"]) == (20, p)
    q = free_price("CHAIWALA", p + 150)
    a_sell, b_buy = a.place("SELL", 10, q), b.place("BUY", 10, q + 5)
    settled(a, a_sell["order_id"])
    settled(b, b_buy["order_id"])
    pa, pb = a.portfolio(), b.portfolio()
    assert (pa["cash_paise"], pa["reserved_cash_paise"]) == (a0["cash_paise"] - 20 * p + 10 * q, 0)
    assert (pb["cash_paise"], pb["reserved_cash_paise"]) == (b0["cash_paise"] - 10 * q, 0)
    assert held(pa, "CHAIWALA") == held(a0, "CHAIWALA") + 10
    assert held(pb, "CHAIWALA") == held(b0, "CHAIWALA") + 10
    TESTERS.update({a.trader_id, b.trader_id})


# ------------------------------------------------------------------ 2


def test_crash_recovery_by_hash() -> None:
    ticker = "DABBA"
    rng = random.Random(42)  # noqa: S311 (a reproducible workload, not cryptography)
    testers = [tester(13 + n) for n in range(3)]
    bots = [bot("bot-11"), bot("bot-12")]
    everyone = testers + bots
    TESTERS.update(t.trader_id for t in testers)
    cancel_everything(everyone)  # orders left by an earlier, interrupted run
    start = {t.trader_id: t.portfolio() for t in everyone}
    sequence_before = live_book(ticker)["sequence"]
    placed: list[tuple[Trader, str]] = []

    def workload() -> None:
        for n in range(160):
            trader = rng.choice(everyone)
            side = "SELL" if trader in bots and rng.random() < 0.6 else "BUY"
            price = 41275 + 5 * rng.randint(-6, 6)
            try:
                placed.append(
                    (trader, trader.place(side, rng.randint(1, 15), price, ticker)["order_id"])
                )
            except Exception as exc:  # a 422 (no cash or shares) is fine; anything else is not
                if getattr(exc, "status", None) != 422:
                    raise
            if n == 80:
                killer.start()

    killer = threading.Thread(target=restart_engine)
    worker = threading.Thread(target=workload)
    worker.start()
    worker.join()
    killer.join()
    assert len(placed) > 100

    def all_matched() -> bool:
        return all(not t.orders("PENDING") for t in everyone)

    wait_for("every order to be processed after the crash", all_matched, timeout=90, every=1)
    before = live_book(ticker)
    restart_engine()  # an idle kill -9: the book must be rebuilt exactly
    after = live_book(ticker)
    assert after["state_hash"] == before["state_hash"], "the rebuilt book differs"
    assert after["sequence"] == before["sequence"]

    # No trade appears twice, and every order's fills agree with the engine's trades.
    trades = [
        e["data"]
        for e in engine_events(ticker, sequence_before)
        if e["event_type"] == "TradeExecuted"
    ]
    assert len({t["trade_id"] for t in trades}) == len(trades)
    filled: dict[str, int] = {}
    for t in trades:
        for order_id in (t["buy_order_id"], t["sell_order_id"]):
            filled[order_id] = filled.get(order_id, 0) + t["quantity"]
    ours = {o["order_id"]: o for trader in everyone for o in trader.orders()}
    for _, order_id in placed:
        assert ours[order_id]["filled_quantity"] == filled.get(order_id, 0), order_id

    # Cancel what rests; then every trader's cash and shares are exactly what the trades say.
    cancel_everything(everyone)
    for trader in everyone:
        cash, shares = start[trader.trader_id]["cash_paise"], held(start[trader.trader_id], ticker)
        for t in trades:
            if t["buyer_trader_id"] == trader.trader_id:
                cash, shares = cash - t["quantity"] * t["price_paise"], shares + t["quantity"]
            if t["seller_trader_id"] == trader.trader_id:
                cash, shares = cash + t["quantity"] * t["price_paise"], shares - t["quantity"]
        now = trader.portfolio()
        assert (now["cash_paise"], held(now, ticker)) == (cash, shares), trader.nickname
        assert now["reserved_cash_paise"] == 0, trader.nickname
    print(
        f"     {len(placed)} orders, {len(trades)} trades; book hash"
        f" {after['state_hash'][:16]}… before and after kill -9"
    )


def cancel_everything(traders: list[Trader]) -> None:
    for trader in traders:
        for order in trader.orders("open"):
            if not order["cancel_requested"]:
                trader.cancel(order["order_id"])
    wait_for("the cancels", lambda: all(not t.orders("open") for t in traders), timeout=30, every=1)


def held(portfolio: dict[str, Any], ticker: str) -> int:
    return sum(h["quantity"] for h in portfolio["holdings"] if h["ticker"] == ticker)


# ------------------------------------------------------------------ 3


def test_orphan_rule_is_fail_closed() -> None:
    c = tester(16)
    TESTERS.add(c.trader_id)
    cancel_everything([c])
    start_cash = c.portfolio()["cash_paise"]
    # (a) order-service "died" after reserving and before storing: only the reservation exists.
    lost_order = str(uuid.uuid4())
    http(
        "POST",
        f"{PORTFOLIO}/internal/v1/reservations",
        {
            "order_id": lost_order,
            "trader_id": c.trader_id,
            "ticker": "CHAIWALA",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 10,
            "limit_price_paise": 24600,
        },
    )
    # (b) an order that cannot be published: Redpanda is down.
    compose("stop", "redpanda")
    began = time.monotonic()
    try:
        stuck = c.place("BUY", 5, free_price("CHAIWALA", 24000))  # can never meet a resting ask
        assert stuck["status"] == "PENDING"
        rejected = wait_for(
            "the unpublished order to be REJECTED",
            lambda: (o := c.order(stuck["order_id"]))["status"] == "REJECTED" and o,
            timeout=75,
            every=1,
        )
        took = (parse(rejected["updated_at"]) - parse(rejected["placed_at"])).total_seconds()
        assert rejected["rejection_reason"] == "PUBLISH_TIMEOUT"
        assert 60 <= took <= 65, f"rejected {took:.1f} s after it was placed"
        print(f"     unpublished order REJECTED (PUBLISH_TIMEOUT) {took:.1f} s after it was placed")
        # Past 120 s nothing may be released: the engine cannot answer while Redpanda is down.
        while time.monotonic() - began < 140:
            time.sleep(2)
        held = c.portfolio()["reserved_cash_paise"]
        expected = 10 * 24600 + 5 * stuck["limit_price_paise"]
        assert held == expected, f"released without the engine's answer: {held}"
        waited = time.monotonic() - began
        print(f"     after {waited:.0f} s both reservations are still held (fail-closed)")
    finally:
        compose("start", "redpanda")
    wait_for(
        "the money to be released",
        lambda: c.portfolio()["reserved_cash_paise"] == 0,
        timeout=180,
        every=2,
    )
    print(
        f"     released {time.monotonic() - began:.0f} s after the start, once the engine answered"
    )
    events = engine_events("CHAIWALA")
    lost = [e["event_type"] for e in events if e["data"].get("order_id") == lost_order]
    assert lost == ["OrderExpired"], lost  # never seen by the engine: a tombstone
    late = [
        (e["event_type"], e["data"].get("reason"))
        for e in events
        if e["data"].get("order_id") == stuck["order_id"]
    ]
    # The Kafka client gave up on the hung send 10 s after it was handed over, so it never
    # reached the engine: only the tombstone. (Before the Phase 3 fix the client kept it and
    # delivered it when Redpanda returned: OrderAccepted, then OrderCancelled ORDER_EXPIRED.)
    assert late == [("OrderExpired", None)], late
    print(f"     the engine answered the unpublished order with: {', '.join(k for k, _ in late)}")

    # A late copy of the lost order (a relay that hung): the tombstone refuses it.
    produce(
        "orders.v1",
        "CHAIWALA",
        {
            "event_id": str(uuid.uuid4()),
            "event_type": "OrderPlaced",
            "event_version": 1,
            "occurred_at": now_iso(),
            "producer": "order-service",
            "environment": "dev",
            "correlation_id": "e2e-zombie",
            "causation_id": None,
            "data": {
                "order_id": lost_order,
                "trader_id": c.trader_id,
                "ticker": "CHAIWALA",
                "side": "BUY",
                "order_type": "LIMIT",
                "quantity": 10,
                "limit_price_paise": 24600,
                "protection_price_paise": None,
                "reservation_id": str(uuid.uuid4()),
                "placed_at": now_iso(),
            },
        },
    )
    time.sleep(5)
    kinds = [
        e["event_type"]
        for e in engine_events("CHAIWALA")
        if e["data"].get("order_id") == lost_order
    ]
    assert kinds == ["OrderExpired"], kinds
    assert c.portfolio()["cash_paise"] == start_cash


def test_bots_keep_a_sound_market() -> None:
    """Phase 3: with the 20 bots trading for 60 s, every ticker has 5s candles with trades, the
    book is never crossed (engine and market-data), no bot order is refused, no 5xx answers."""
    root = Path(__file__).resolve().parents[1]
    subprocess.run(["make", "-s", "bots", "seed=42"], cwd=root, check=True)  # noqa: S603, S607
    try:
        time.sleep(10)  # the market makers' first quotes, the first candles
        check = subprocess.run(  # noqa: S603
            [sys.executable, str(root / "scripts" / "bots_check.py"), "60"], cwd=root, check=False
        )
        assert check.returncode == 0, "the market was not sound with the bots running"
    finally:
        subprocess.run(["make", "-s", "bots-stop"], cwd=root, check=True)  # noqa: S603, S607


def test_test_traders_are_never_ranked() -> None:
    """Phase 4, projector safety: what the tests did never shows on the leaderboard, and A-01
    refuses a nickname on the blocklist before any account exists."""
    viewer = bot("bot-20")
    everyone = http(
        "GET", f"{GATEWAY}/v1/leaderboard?limit=100&include_bots=true", headers=viewer.auth
    )["entries"]
    ranked = {e["trader_id"] for e in everyone}
    assert TESTERS, "the earlier tests made their traders"
    assert not ranked & TESTERS, "a test trader is on the leaderboard"
    assert {e["kind"] for e in everyone} <= {"HUMAN", "BOT"}
    try:
        http("POST", f"{GATEWAY}/v1/sessions", {"nickname": "B1tch_e2e"})
        raise AssertionError("a blocklisted nickname joined")
    except ApiError as refused:
        assert (refused.status, refused.body["code"]) == (422, "VALIDATION_FAILED")
    print(f"     {len(TESTERS)} test traders, none of {len(everyone)} ranked traders")


TESTERS: set[str] = set()  # trader IDs the tests made

TESTS: list[Callable[[], None]] = [
    test_place_trade_settle,
    test_crash_recovery_by_hash,
    test_orphan_rule_is_fail_closed,
    test_bots_keep_a_sound_market,
    test_test_traders_are_never_ranked,
]


def main() -> int:
    wait_until_ready()
    failed = 0
    for test in TESTS:
        began = time.monotonic()
        print(f"  ... {test.__name__}", flush=True)
        try:
            test()
            print(f"  PASS {test.__name__} ({time.monotonic() - began:.0f} s)", flush=True)
        except Exception:
            failed += 1
            print(f"  FAIL {test.__name__}", flush=True)
            traceback.print_exc()
    print(f"\n{len(TESTS) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
