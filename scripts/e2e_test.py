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
4. Phase 3: the 20 bots trade for 120 s (make bots): candles for all 5 tickers, the book never
   crossed, no refused bot order, no 5xx, the trade-rate floor, a two-sided book and every price
   within ±10% of its reference. The bots keep trading through the Phase 4 checks below.
5. Phase 4, time travel (docs/03-architecture/04-event-sourcing-and-cqrs.md §5): the live engine
   book (Q-16) recorded at three moments is rebuilt exactly from the event store later (Q-12,
   same sequence and state hash); a rewind to now is verified against the live engine for all
   five tickers; a replay (Q-13) ends on the hash Q-12 rebuilds at its end and plays exactly the
   fills the engine made; four replays at once (three traders and the presenter) leave live
   trading alone: a fifth trader is told to come back (1013), the presenter never is, the live
   books still verify, and placing an order stays fast (REPLAY_P95_BOUND_MS).
6. Phase 4: no trader a test made is ever ranked on the leaderboard (Q-11), and A-01 refuses a
   nickname on the blocklist without opening an account.

Takes about 7 minutes (the orphan rule waits for real 60 s and 120 s timers; the bots run 120 s,
then on through the time-travel checks).
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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from stack import (
    ENGINE,
    GATEWAY,
    PORTFOLIO,
    ApiError,
    Replay,
    Trader,
    WebSocket,
    admin_token,
    bot,
    compose,
    engine_events,
    free_price,
    http,
    live_book,
    now_iso,
    open_replay,
    play,
    produce,
    settled,
    tester,
    wait_for,
    wait_until_ready,
)

FINAL = ("FILLED", "CANCELLED", "REJECTED")
ROOT = Path(__file__).resolve().parents[1]
TICKERS = ("CHAIWALA", "JUGAAD", "DABBA", "MONSOON", "FILMY")
# Placing an order while four replays run. Measured on the dev laptop with the bots trading
# (2026-11-12): p95 24 ms without replays, 20 ms during four, so replays cost nothing visible.
# The bound leaves room for a slower CI runner.
REPLAY_P95_BOUND_MS = 500


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


BOTS = {"started": False}


def start_bots() -> None:
    """The 20 bots (seed 42), once; main() stops them after the last test."""
    if not BOTS["started"]:
        subprocess.run(["make", "-s", "bots", "seed=42"], cwd=ROOT, check=True)  # noqa: S603, S607
        BOTS["started"] = True
        time.sleep(10)  # the market makers' first quotes, the first candles


def test_bots_keep_a_sound_market() -> None:
    """Phase 3: with the 20 bots trading for 120 s, every ticker has 5s candles with trades, the
    book is never crossed (engine and market-data), no bot order is refused, no 5xx answers,
    and every price stays within ±10% of its reference (Phase 4). 120 s, not 60: from a cold
    start a 60 s sample of the averages landed just past a threshold in 2 of 3 CI runs (trades
    3.2/min against 5; one maker side 3.4 quotes against 3.5) while the market was sound."""
    start_bots()
    check = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "scripts" / "bots_check.py"), "120"], cwd=ROOT, check=False
    )
    assert check.returncode == 0, "the market was not sound with the bots running"


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def at(viewer: Trader, ticker: str, moment: str) -> dict[str, Any]:
    """Q-12 through the gateway: the market of `ticker` rebuilt at `moment`."""
    result: dict[str, Any] = http(
        "GET", f"{GATEWAY}/v1/time-travel/{ticker}?at={moment}", headers=viewer.auth
    )
    return result


def next_event(ticker: str, sequence: int) -> dict[str, Any] | None:
    page = http(
        "GET", f"{ENGINE}/internal/v1/events?ticker={ticker}&after_sequence={sequence}&limit=1"
    )
    event: dict[str, Any] | None = page["events"][0] if page["events"] else None
    return event


def test_time_travel_rebuilds_three_moments_exactly() -> None:
    """Phase 4: with the bots trading, the live engine book (Q-16) is recorded at three moments.
    Rebuilt later from snapshots and events (Q-12), each moment has the same sequence and state
    hash. A moment counts only once the engine's next event is later than it, so the time names
    exactly that sequence (two commands can share a millisecond)."""
    start_bots()
    viewer = tester(17)
    TESTERS.add(viewer.trader_id)
    moments = []
    for ticker in ("CHAIWALA", "DABBA", "MONSOON"):
        while True:
            time.sleep(3)
            book = live_book(ticker)
            after = wait_for(
                f"the next {ticker} event",
                lambda t=ticker, s=book["sequence"]: next_event(t, s),
                timeout=60,
            )
            if after["occurred_at"] != book["as_of"]:
                break
        moments.append(book)
    time.sleep(3)  # the market moves on before the rewind
    for book in moments:
        then = at(viewer, book["ticker"], book["as_of"])
        assert (then["as_of_sequence"], then["state_hash"]) == (
            book["sequence"],
            book["state_hash"],
        ), f"{book['ticker']} rebuilt at {book['as_of']} differs from the live book then"
        print(
            f"     {book['ticker']:<8} at {book['as_of']}: sequence {book['sequence']},"
            f" hash {book['state_hash'][:12]}... rebuilt exactly"
        )


def test_rewind_to_now_is_verified_against_the_live_engine() -> None:
    """Phase 4: asking for "now" (a second ahead, as the screen does) rebuilds to the sequence the
    live engine has and compares hashes, on every ticker, while the bots keep trading."""
    start_bots()
    viewer = tester(17)
    for ticker in TICKERS:
        before = live_book(ticker)["sequence"]
        answer = at(viewer, ticker, iso(datetime.now(UTC) + timedelta(seconds=1)))
        proof = answer["verification"]
        assert proof is not None and proof["matches"], f"{ticker}: {proof}"
        assert proof["live_sequence"] >= before
        assert (answer["as_of_sequence"], answer["state_hash"]) == (
            proof["live_sequence"],
            proof["live_state_hash"],
        )
        print(f"     {ticker:<8} verified at sequence {proof['live_sequence']}: hashes match")


def test_a_replay_ends_on_the_rebuilt_hash_and_plays_every_fill() -> None:
    """Phase 4: two minutes of bot trading replayed at 60x. The end message carries the hash Q-12
    rebuilds at `to`, the first frame is the book at `from`, and the fills in the frames are
    exactly the engine's TradeExecuted events in between, in order."""
    start_bots()
    viewer = tester(18)
    TESTERS.add(viewer.trader_id)
    now = datetime.now(UTC)
    start, until = iso(now - timedelta(seconds=150)), iso(now - timedelta(seconds=30))
    began = time.monotonic()
    replay = play(open_replay(viewer.token, "CHAIWALA", start, until, 60))
    took = time.monotonic() - began
    assert replay.close_code == 1000 and replay.end is not None, replay.close_code
    first, last = at(viewer, "CHAIWALA", start), at(viewer, "CHAIWALA", until)
    assert (replay.end["as_of_sequence"], replay.end["state_hash"]) == (
        last["as_of_sequence"],
        last["state_hash"],
    )
    assert replay.frames[0]["as_of_sequence"] == first["as_of_sequence"]
    assert replay.end["frames_sent"] == len(replay.frames)
    replayed = [t["trade_id"] for f in replay.frames for t in f.get("trades") or []]
    live = [
        e["data"]["trade_id"]
        for e in engine_events("CHAIWALA", after=first["as_of_sequence"])
        if e["event_type"] == "TradeExecuted"
        and e["data"]["sequence"] <= replay.end["as_of_sequence"]
    ]
    assert live, "no trades in the window: are the bots trading?"
    assert replayed == live, "the replay's tape differs from the engine's fills"
    print(
        f"     {len(replay.frames)} frames and {len(live)} fills in {took:.1f} s at 60x;"
        f" end hash {replay.end['state_hash'][:12]}... == Q-12 at `to`"
    )


def order_latencies(trader: Trader, ticker: str, count: int) -> list[float]:
    """Milliseconds for A-05 (place) of `count` orders that rest far below the market, each
    cancelled at once. Paced at about 8 requests a second, under the gateway's 20 per trader, so
    no rate-limit wait is counted as latency."""
    price = max(5, live_book(ticker)["bids"][0]["price_paise"] * 6 // 10 // 5 * 5)
    took = []
    for _ in range(count):
        began = time.monotonic()
        order = trader.place("BUY", 1, price, ticker)
        took.append((time.monotonic() - began) * 1000)
        trader.cancel(order["order_id"])
        time.sleep(0.2)
    return took


def p95(values: list[float]) -> float:
    return sorted(values)[max(0, round(0.95 * len(values)) - 1)]


def test_replays_are_isolated_from_live_trading() -> None:
    """Phase 4: replays only read. Four at once while the bots trade (the most one market-data
    replica plays): three traders take the three open places, a fourth trader is told to come
    back later (1013), and the presenter still gets the place kept for the admin role. Each ends
    on the hash Q-12 rebuilds at its end; afterwards every live book still verifies; and placing
    an order meanwhile stays under REPLAY_P95_BOUND_MS at the 95th percentile."""
    start_bots()
    trader = tester(19)
    watchers = [tester(n) for n in (23, 24, 25, 26)]
    for t in (trader, *watchers):
        TESTERS.add(t.trader_id)
    quiet = order_latencies(trader, "JUGAAD", 30)

    now = datetime.now(UTC)
    start, until = iso(now - timedelta(minutes=6)), iso(now - timedelta(minutes=1))
    tickers = ("CHAIWALA", "DABBA", "MONSOON", "FILMY")
    sockets: list[WebSocket] = []
    for watcher, ticker in zip(watchers[:3], tickers, strict=False):
        sockets.append(open_replay(watcher.token, ticker, start, until, 10))
        assert sockets[-1].receive() is not None  # playing: its place is taken
    refused = play(open_replay(watchers[3].token, "FILMY", start, until, 10))
    assert refused.close_code == 1013, f"a fourth trader replay got {refused.close_code}"
    sockets.append(open_replay(admin_token(), "FILMY", start, until, 10))  # the presenter
    assert sockets[-1].receive() is not None

    results: list[Replay | None] = [None] * len(sockets)

    def finish(n: int) -> None:
        results[n] = play(sockets[n])

    readers = [threading.Thread(target=finish, args=(n,)) for n in range(len(sockets))]
    for r in readers:
        r.start()
    busy = order_latencies(trader, "JUGAAD", 30)
    for r in readers:
        r.join(timeout=120)

    viewer = watchers[0]
    for ticker, result in zip(tickers, results, strict=True):
        assert result is not None and result.close_code == 1000 and result.end, ticker
        rebuilt = at(viewer, ticker, until)
        assert result.end["state_hash"] == rebuilt["state_hash"], f"{ticker} replay end hash"
    for ticker in TICKERS:
        proof = at(viewer, ticker, iso(datetime.now(UTC) + timedelta(seconds=1)))["verification"]
        assert proof is not None and proof["matches"], f"{ticker} live book after the replays"
    print(
        f"     placing an order: p95 {p95(quiet):.0f} ms without replays,"
        f" {p95(busy):.0f} ms during four (bound {REPLAY_P95_BOUND_MS} ms);"
        f" a fourth trader replay was refused with 1013, the presenter's was not"
    )
    assert p95(busy) <= REPLAY_P95_BOUND_MS


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
    test_time_travel_rebuilds_three_moments_exactly,
    test_rewind_to_now_is_verified_against_the_live_engine,
    test_a_replay_ends_on_the_rebuilt_hash_and_plays_every_fill,
    test_replays_are_isolated_from_live_trading,
    test_test_traders_are_never_ranked,
]


def main() -> int:
    wait_until_ready()
    failed = 0
    try:
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
    finally:
        if BOTS["started"]:
            subprocess.run(["make", "-s", "bots-stop"], cwd=ROOT, check=True)  # noqa: S603, S607
    print(f"\n{len(TESTS) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
