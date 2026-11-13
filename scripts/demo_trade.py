#!/usr/bin/env python3
"""make demo-trade: two traders and a bot trade through the public gateway, end to end.

1. Two traders, A and B, and bot-01 start sessions (A-03). A and B are load-test accounts
   (load-21, load-22): they start like humans, with ₹1,00,000 and no shares, but are never ranked
   on the leaderboard, so a demo trade never shows on the projector. They keep what they traded
   until make demo-reset. Bots also have shares (no short selling, so someone must sell first).
2. bot-01 sells 20 CHAIWALA at about ₹245.50; A bids 50 paise more → they trade at bot-01's price.
3. A sells 10 at about ₹247.00; B bids 50 paise more → A and B trade at A's price.

Prices are picked inside the book's gap (above every resting bid, below every ask), so orders other
people left in the book cannot take part.

Then it prints the trades (from the engine's event store), the three portfolios and the order
statuses. Needs `make up`.
"""

from __future__ import annotations

import sys

from stack import (
    GATEWAY,
    ApiError,
    Trader,
    bot,
    engine_events,
    free_price,
    live_book,
    rupees,
    settled,
    tester,
    wait_until_ready,
)

TICKER = "CHAIWALA"
BOLD, DIM, GREEN, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[0m"


def heading(text: str) -> None:
    print(f"\n{BOLD}{text}{RESET}")


def show_order(who: str, order: dict[str, object]) -> None:
    price = order["limit_price_paise"]
    avg = order["average_fill_price_paise"]
    limit = rupees(int(price)) if price else "MARKET"
    average = rupees(int(avg)) if avg else "-"
    print(
        f"  {who:<10} {order['side']:<4} {order['quantity']:>3} @ {limit:>9}"
        f"   {GREEN}{order['status']:<16}{RESET} filled {order['filled_quantity']:>3}"
        f"   average {average:>9}"
    )


def show_portfolio(who: str, trader: Trader) -> None:
    p = trader.portfolio()
    shares = {h["ticker"]: h for h in p["holdings"]}.get(TICKER)
    held = f"{shares['quantity']:>5} {TICKER}" if shares else f"{0:>5} {TICKER}"
    cash, reserved = rupees(p["cash_paise"]), rupees(p["reserved_cash_paise"])
    worth, pnl = rupees(p["net_worth_paise"]), rupees(p["pnl_paise"])
    print(
        f"  {who:<10} cash {cash:>14}   reserved {reserved:>10}"
        f"   {held}   net worth {worth:>14}   P&L {pnl:>10}"
    )


def main() -> int:
    heading(f"CircuitBreaker — demo trade (everything through the public API at {GATEWAY})")
    wait_until_ready()
    start_sequence = live_book(TICKER)["sequence"]

    a, b, seller = tester(21), tester(22), bot("bot-01")
    print(
        f"  sessions: {a.nickname} (A), {b.nickname} (B), {seller.nickname}"
        "   (A and B are load-test accounts: never on the leaderboard)"
    )

    p = free_price(TICKER, 24550)
    heading(
        f"1. bot-01 sells 20 at {rupees(p)}; A bids {rupees(p + 50)} (and pays the resting price)"
    )
    sell = seller.place("SELL", 20, p)
    buy = a.place("BUY", 20, p + 50)
    print(f"  {DIM}both answered 202 PENDING: matching is asynchronous{RESET}")
    sell, buy = settled(seller, sell["order_id"]), settled(a, buy["order_id"])

    q = free_price(TICKER, p + 150)
    heading(f"2. A sells 10 of those shares at {rupees(q)}; B bids {rupees(q + 50)}")
    a_sell = a.place("SELL", 10, q)
    b_buy = b.place("BUY", 10, q + 50)
    a_sell, b_buy = settled(a, a_sell["order_id"]), settled(b, b_buy["order_id"])

    heading("Trades (from the matching engine's event store)")
    names = {a.trader_id: "A", b.trader_id: "B", seller.trader_id: "bot-01"}
    for e in engine_events(TICKER, start_sequence):
        if e["event_type"] == "TradeExecuted":
            d = e["data"]
            buyer, sold_by = (
                names.get(d["buyer_trader_id"], "other"),
                names.get(d["seller_trader_id"], "other"),
            )
            price, value = rupees(d["price_paise"]), rupees(d["quantity"] * d["price_paise"])
            print(
                f"  #{d['sequence']:<5} {sold_by:>6} → {buyer:<6} {d['quantity']:>3} {TICKER}"
                f" @ {price}   = {value}"
            )

    heading("Orders")
    for who, trader, order in (
        ("bot-01", seller, sell),
        ("A", a, buy),
        ("A", a, a_sell),
        ("B", b, b_buy),
    ):
        show_order(who, trader.order(order["order_id"]))

    heading("Portfolios (cash includes reserved cash; shares valued at the last trade price)")
    for who, trader in (("A", a), ("B", b), ("bot-01", seller)):
        show_portfolio(who, trader)
    print(
        f"\n  A paid {rupees(20 * p)} for 20 shares and got {rupees(10 * q)} for 10;"
        f" B paid {rupees(10 * q)}.\n  Price improvement (offered more than paid) went"
        " back to the buyers: nothing stays reserved.\n"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ApiError as exc:
        print(f"\nThe API refused a step: {exc}", file=sys.stderr)
        sys.exit(1)
    except (TimeoutError, OSError) as exc:
        print(f"\nIs the stack running (make up)? {exc}", file=sys.stderr)
        sys.exit(1)
