#!/usr/bin/env python3
"""The PostSync smoke test (ADR-0014): after Argo CD syncs a service, check the environment end to
end through the public API, as a client would.

    --mode trade          staging: two load-test accounts trade one CALVERA share with each other
    --mode place-cancel   prod: a load-test account places a LIMIT buy far below the market, then
                          cancels it: nothing trades and nothing stays reserved

It runs as a Job inside the namespace after every sync of a service (the chart's smoke-job.yaml,
Argo CD's PostSync hook), or from the laptop: python3 scripts/k8s.py run --env staging smoke.py.
The accounts are LOAD accounts (load-NN, A-03), never ranked on the leaderboard, so a smoke test
never reaches the projector; each service's Job has its own (--account N), so Jobs running at
the same time never meet in the book. In trade mode the pair hands one share back and forth: the
first run buys it from bot-20 (no short selling, so a share must come from somewhere).

Standard library only (it runs on a plain python image). Exit 0 = passed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

GATEWAY = os.environ.get("GATEWAY", "http://api-gateway:8000")
BOT_SECRET = os.environ.get("BOT_SECRET", "")
TICKER = "CALVERA"
TICK = 5
RUN = f"smoke-{uuid.uuid4().hex[:8]}"  # every request's correlation ID: one search in Loki


class ApiError(Exception):
    def __init__(self, status: int, body: Any) -> None:
        super().__init__(f"HTTP {status}: {body}")
        self.status = status


def http(method: str, path: str, body: Any = None, headers: dict[str, str] | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(  # noqa: S310 (the gateway's http:// URL inside the cluster)
        f"{GATEWAY}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "X-Correlation-Id": RUN, **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="replace")
        try:
            raise ApiError(e.code, json.loads(text)) from None
        except json.JSONDecodeError:
            raise ApiError(e.code, text) from None


def check(ok: bool, what: str) -> None:
    print(f"  {'PASS' if ok else 'FAIL'} {what}", flush=True)
    if not ok:
        sys.exit(1)


def wait_for(what: str, probe: Any, timeout: float = 30, every: float = 0.5) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        try:
            result = probe()
        except (ApiError, OSError):
            result = None
        if result:
            return result
        if time.monotonic() > deadline:
            check(False, f"{what} (gave up after {timeout:.0f} s)")
        time.sleep(every)


class Trader:
    def __init__(self, account: str) -> None:
        session = http(
            "POST", "/v1/bot-sessions", {"account_name": account}, {"X-Bot-Secret": BOT_SECRET}
        )
        self.name = account
        self.auth = {"Authorization": f"Bearer {session['access_token']}"}

    def place(self, side: str, price: int) -> str:
        """A-05 with an Idempotency-Key, retried with the same key on 503 or a timeout."""
        order = {
            "ticker": TICKER,
            "side": side,
            "order_type": "LIMIT",
            "quantity": 1,
            "limit_price_paise": price,
        }
        headers = {**self.auth, "Idempotency-Key": str(uuid.uuid4())}
        for pause in (1, 2, 4, None):
            try:
                return str(http("POST", "/v1/orders", order, headers)["order_id"])
            except (ApiError, OSError) as e:
                if pause is None or (isinstance(e, ApiError) and e.status != 503):
                    raise
                time.sleep(pause)
        raise AssertionError("unreachable")

    def order(self, order_id: str) -> dict[str, Any]:
        result: dict[str, Any] = http("GET", f"/v1/orders/{order_id}", headers=self.auth)
        return result

    def status_becomes(self, order_id: str, statuses: tuple[str, ...]) -> dict[str, Any]:
        result: dict[str, Any] = wait_for(
            f"{self.name}'s order becomes {'/'.join(statuses)}",
            lambda: (o := self.order(order_id))["status"] in statuses and o,
        )
        return result

    def cancel(self, order_id: str) -> None:
        http("DELETE", f"/v1/orders/{order_id}", headers=self.auth)

    def portfolio(self) -> dict[str, Any]:
        result: dict[str, Any] = http("GET", "/v1/portfolio", headers=self.auth)
        return result

    def shares(self) -> int:
        held = {h["ticker"]: h["quantity"] for h in self.portfolio()["holdings"]}
        return int(held.get(TICKER, 0))


def ready(trader_account: str, wait: float) -> Trader:
    """The environment answers: a session, the five instruments, every order book. A service that
    was just synced may still be starting elsewhere, so wait for it."""

    def probe() -> Trader | None:
        trader = Trader(trader_account)
        instruments = http("GET", "/v1/instruments", headers=trader.auth)["instruments"]
        tickers = [i["ticker"] for i in instruments]
        for ticker in tickers:
            http("GET", f"/v1/market/{ticker}/order-book", headers=trader.auth)
        return trader if len(tickers) == 5 else None

    trader: Trader = wait_for("the gateway, instruments and order books answer", probe, wait, 5)
    check(True, "the gateway, the five instruments and every order book answer")
    return trader


def free_price(trader: Trader) -> int:
    """Inside the book's gap (above every bid, below every ask), near the last price: the smoke
    test's own buy and sell meet each other, not orders someone else left."""
    book = http("GET", f"/v1/market/{TICKER}/order-book", headers=trader.auth)
    last = next(
        i["last_price_paise"]
        for i in http("GET", "/v1/instruments", headers=trader.auth)["instruments"]
        if i["ticker"] == TICKER
    )
    low = book["best_bid_paise"] + TICK if book["best_bid_paise"] else TICK
    high = book["best_ask_paise"] - TICK if book["best_ask_paise"] else 10_000_000
    if low > high:
        check(False, f"a free price in the {TICKER} book (bid and ask touch)")
    return int(min(max(last // TICK * TICK, low), high))


def settled_balance(trader: Trader, cash: int) -> dict[str, Any]:
    """Settlement follows the trade event; wait until the cash says so and nothing is reserved."""
    result: dict[str, Any] = wait_for(
        f"{trader.name}'s cash settles and nothing stays reserved",
        lambda: (
            (p := trader.portfolio())["cash_paise"] == cash and p["reserved_cash_paise"] == 0 and p
        ),
        timeout=20,
    )
    return result


def trade(account: int, wait: float) -> None:
    a = ready(f"load-{account:02d}", wait)
    b = Trader(f"load-{account + 1:02d}")
    if a.shares() == 0 and b.shares() == 0:
        source = Trader("bot-20")
        price = free_price(a)
        sold = source.place("SELL", price)
        source.status_becomes(sold, ("OPEN",))
        bought = a.place("BUY", price)
        source.status_becomes(sold, ("FILLED",))
        a.status_becomes(bought, ("FILLED",))
        print(f"  first run: {a.name} bought a share from bot-20 to hand back and forth")
    seller, buyer = (a, b) if a.shares() > 0 else (b, a)
    seller_start, buyer_start = seller.portfolio(), buyer.portfolio()
    seller_shares, buyer_shares = seller.shares(), buyer.shares()

    price = free_price(buyer)
    bid = buyer.place("BUY", price)
    buyer.status_becomes(bid, ("OPEN",))  # resting, so the sell below meets it
    ask = seller.place("SELL", price)
    seller.status_becomes(ask, ("FILLED",))
    buyer.status_becomes(bid, ("FILLED",))
    check(True, f"{seller.name} sold 1 {TICKER} to {buyer.name} at {price} paise")

    settled_balance(buyer, buyer_start["cash_paise"] - price)
    settled_balance(seller, seller_start["cash_paise"] + price)
    check(
        buyer.shares() == buyer_shares + 1 and seller.shares() == seller_shares - 1,
        "both portfolios settled exactly: cash and shares moved, nothing reserved",
    )


def place_cancel(account: int, wait: float) -> None:
    trader = ready(f"load-{account:02d}", wait)
    start = trader.portfolio()
    if start["reserved_cash_paise"] != 0:
        check(False, f"{trader.name} starts with nothing reserved")
    last = next(
        i["last_price_paise"]
        for i in http("GET", "/v1/instruments", headers=trader.auth)["instruments"]
        if i["ticker"] == TICKER
    )
    price = max(TICK, last // 2 // TICK * TICK)  # half the last price: far below any seller
    order_id = trader.place("BUY", price)
    trader.status_becomes(order_id, ("OPEN",))
    reserved = trader.portfolio()["reserved_cash_paise"]
    check(reserved == price, f"a LIMIT buy at {price} paise rests in the book, {price} reserved")
    trader.cancel(order_id)
    trader.status_becomes(order_id, ("CANCELLED",))
    settled_balance(trader, start["cash_paise"])
    check(True, "cancelled: nothing traded, the reservation released, cash unchanged")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--mode", choices=["trade", "place-cancel"], required=True)
    parser.add_argument("--account", type=int, default=71, help="load-NN (trade uses N and N+1)")
    parser.add_argument("--wait", type=float, default=300, help="seconds to wait for readiness")
    args = parser.parse_args()
    if not BOT_SECRET:
        sys.exit("BOT_SECRET is not set")
    print(f"smoke test ({args.mode}) against {GATEWAY}, correlation ID {RUN}", flush=True)
    started = time.monotonic()
    (trade if args.mode == "trade" else place_cancel)(args.account, args.wait)
    print(f"passed in {time.monotonic() - started:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
