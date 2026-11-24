#!/usr/bin/env python3
"""The PostSync smoke test (ADR-0014): after Argo CD syncs a service, check the environment end to
end through the public API, as a client would.

    --mode trade          staging: two load-test accounts trade one share with each other
    --mode place-cancel   prod: a load-test account places a LIMIT buy far below the market, then
                          cancels it: nothing trades and nothing stays reserved

It runs as a Job inside the namespace after every sync of a service (the chart's smoke-job.yaml,
Argo CD's PostSync hook), or from the laptop: python3 scripts/k8s.py run --env staging smoke.py.
The accounts are LOAD accounts (load-NN, A-03), never ranked on the leaderboard, so a smoke test
never reaches the projector. Each service's Job has its own (--account N) and, from it, its own
ticker, but nine Jobs share five books, and a book matches by price and time, not by account: when
Argo CD syncs several services at once, one Job's sell can meet another Job's buy (found by CI's
platform check, 2026-11-24). So an attempt that does not fill as planned cancels its own orders and
tries again after a random pause; an account's cash only moves with its own orders, so the
settlement check stays exact whichever order it met. In trade mode the pair hands one share back
and forth: the first run buys it from bot-20 (no short selling, so a share must come from
somewhere).

Standard library only (it runs on a plain python image). Exit 0 = passed.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

GATEWAY = os.environ.get("GATEWAY", "http://api-gateway:8000")
BOT_SECRET = os.environ.get("BOT_SECRET", "")
TICKERS = ["CALVERA", "KORVIN", "ASTRYX", "HELIOR", "TAVIRA"]
TICK = 5
ATTEMPTS = 5
FINISHED = ("FILLED", "CANCELLED", "REJECTED")
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
    def __init__(self, account: str, ticker: str) -> None:
        session = http(
            "POST", "/v1/bot-sessions", {"account_name": account}, {"X-Bot-Secret": BOT_SECRET}
        )
        self.name = account
        self.ticker = ticker
        self.auth = {"Authorization": f"Bearer {session['access_token']}"}

    def place(self, side: str, price: int) -> str:
        """A-05 with an Idempotency-Key, retried with the same key on 503 or a timeout."""
        order = {
            "ticker": self.ticker,
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

    def status_becomes(
        self, order_id: str, statuses: tuple[str, ...], timeout: float = 30
    ) -> dict[str, Any]:
        result: dict[str, Any] = wait_for(
            f"{self.name}'s order becomes {'/'.join(statuses)}",
            lambda: (o := self.order(order_id))["status"] in statuses and o,
            timeout,
        )
        return result

    def filled(self, order_id: str, timeout: float) -> dict[str, Any] | None:
        """The order once FILLED, or None if it is still not after timeout seconds."""
        deadline = time.monotonic() + timeout
        while (order := self.order(order_id))["status"] != "FILLED":
            if order["status"] in FINISHED or time.monotonic() > deadline:
                return None
            time.sleep(0.5)
        return order

    def cancel(self, order_id: str) -> None:
        http("DELETE", f"/v1/orders/{order_id}", headers=self.auth)

    def withdraw(self, order_id: str) -> None:
        """Cancel the order unless it has finished, then wait until it has (a fill may win)."""
        if self.order(order_id)["status"] not in FINISHED:
            try:
                self.cancel(order_id)
            except ApiError as e:
                if e.status != 409:  # 409: it finished in the meantime
                    raise
        self.status_becomes(order_id, FINISHED)

    def portfolio(self) -> dict[str, Any]:
        result: dict[str, Any] = http("GET", "/v1/portfolio", headers=self.auth)
        return result

    def shares(self) -> int:
        held = {h["ticker"]: h["quantity"] for h in self.portfolio()["holdings"]}
        return int(held.get(self.ticker, 0))


def ready(trader_account: str, ticker: str, wait: float) -> Trader:
    """The environment answers: a session, the five instruments, every order book. A service that
    was just synced may still be starting elsewhere, so wait for it."""

    def probe() -> Trader | None:
        trader = Trader(trader_account, ticker)
        instruments = http("GET", "/v1/instruments", headers=trader.auth)["instruments"]
        tickers = [i["ticker"] for i in instruments]
        for each in tickers:
            http("GET", f"/v1/market/{each}/order-book", headers=trader.auth)
        return trader if len(tickers) == 5 else None

    trader: Trader = wait_for("the gateway, instruments and order books answer", probe, wait, 5)
    check(True, "the gateway, the five instruments and every order book answer")
    return trader


def free_price(trader: Trader) -> int | None:
    """Inside the book's gap (above every bid, below every ask), near the last price, so the smoke
    test's own buy and sell meet each other; None when bid and ask touch (no gap)."""
    book = http("GET", f"/v1/market/{trader.ticker}/order-book", headers=trader.auth)
    low = book["best_bid_paise"] + TICK if book["best_bid_paise"] else TICK
    high = book["best_ask_paise"] - TICK if book["best_ask_paise"] else 10_000_000
    if low > high:
        return None
    return int(min(max(last_price(trader) // TICK * TICK, low), high))


def last_price(trader: Trader) -> int:
    return int(
        next(
            i["last_price_paise"]
            for i in http("GET", "/v1/instruments", headers=trader.auth)["instruments"]
            if i["ticker"] == trader.ticker
        )
    )


def cross(buyer: Trader, seller: Trader) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """One attempt: the buyer's LIMIT buy rests inside the gap, the seller's sell at the same price
    meets it. Both FILLED: the two orders. Otherwise (no gap, or another Job's order met one of
    ours) cancel what is still open, wait until both have finished, and return None."""
    price = free_price(buyer)
    if price is None:
        print(f"   no gap in the {buyer.ticker} book (bid and ask touch)", flush=True)
        return None
    bid = buyer.place("BUY", price)
    buyer.status_becomes(bid, ("OPEN", *FINISHED))
    ask = seller.place("SELL", price)
    bought, sold = buyer.filled(bid, 10), seller.filled(ask, 10)
    if bought and sold:
        return bought, sold
    print(f"   another order in the {buyer.ticker} book met ours: withdrawing", flush=True)
    buyer.withdraw(bid)
    seller.withdraw(ask)
    return None


def attempts(what: str, attempt: Any) -> Any:
    """Up to ATTEMPTS tries, a random pause between them (Jobs started together drift apart)."""
    for n in range(1, ATTEMPTS + 1):
        if result := attempt():
            return result
        if n < ATTEMPTS:
            time.sleep(random.uniform(1, 5))  # noqa: S311 (a pause, not a secret)
    check(False, f"{what} ({ATTEMPTS} attempts)")
    raise AssertionError("unreachable")


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


def quiet(trader: Trader) -> dict[str, Any]:
    """The portfolio once nothing is reserved: every earlier order of this account has settled
    (a fill that won the race against a cancel settles a moment after the order finished)."""
    result: dict[str, Any] = wait_for(
        f"{trader.name} has nothing reserved",
        lambda: (
            (p := trader.portfolio())["reserved_cash_paise"] == 0
            and all(h["reserved_quantity"] == 0 for h in p["holdings"])
            and p
        ),
        timeout=20,
    )
    return result


def trade(account: int, ticker: str, wait: float) -> None:
    a = ready(f"load-{account:02d}", ticker, wait)
    b = Trader(f"load-{account + 1:02d}", ticker)

    def once() -> bool:
        quiet(a)
        quiet(b)
        if a.shares() == 0 and b.shares() == 0:
            if not cross(a, Trader("bot-20", ticker)):
                return False
            print(f"  the pair held no {ticker}: {a.name} bought a share from bot-20")
            quiet(a)
        seller, buyer = (a, b) if a.shares() > 0 else (b, a)
        seller_start, buyer_start = quiet(seller), quiet(buyer)
        seller_shares, buyer_shares = seller.shares(), buyer.shares()
        if not (orders := cross(buyer, seller)):
            return False
        bought_at = orders[0]["average_fill_price_paise"]
        sold_at = orders[1]["average_fill_price_paise"]
        check(True, f"{seller.name} sold 1 {ticker} at {sold_at} paise, {buyer.name} bought 1 at"
                    f" {bought_at}")  # fmt: skip
        settled_balance(buyer, buyer_start["cash_paise"] - bought_at)
        settled_balance(seller, seller_start["cash_paise"] + sold_at)
        check(
            buyer.shares() == buyer_shares + 1 and seller.shares() == seller_shares - 1,
            "both portfolios settled exactly: cash and shares moved, nothing reserved",
        )
        return True

    attempts("a trade between the pair", once)


def place_cancel(account: int, ticker: str, wait: float) -> None:
    trader = ready(f"load-{account:02d}", ticker, wait)
    start = trader.portfolio()
    if start["reserved_cash_paise"] != 0:
        check(False, f"{trader.name} starts with nothing reserved")
    price = max(TICK, last_price(trader) // 2 // TICK * TICK)  # half the last price: no seller
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
    ticker = TICKERS[args.account // 2 % len(TICKERS)]  # neighbouring Jobs, different books
    print(f"smoke test ({args.mode}, {ticker}) against {GATEWAY}, correlation ID {RUN}", flush=True)
    started = time.monotonic()
    (trade if args.mode == "trade" else place_cancel)(args.account, ticker, args.wait)
    print(f"passed in {time.monotonic() - started:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
