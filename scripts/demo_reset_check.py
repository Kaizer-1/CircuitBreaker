#!/usr/bin/env python3
"""The last step of make demo-reset: prove the fresh stack is in the rehearsal starting state.

- every ticker's book in the matching engine is empty and has no history (sequence 0);
- only the seeded accounts exist: bot-01 ... bot-20 and load-01 ... load-10, no humans;
- every bot has its starting ₹5,00,000 and 1,000 shares of each ticker, nothing reserved.

It reads the engine's internal Q-16 and portfolio's Postgres on purpose (dev host ports only);
the bots' portfolios are read through the public API like any client.
"""

from __future__ import annotations

import subprocess
import sys

from stack import COMPOSE, ENGINE, bot, http, wait_until_ready

TICKERS = ("CALVERA", "KORVIN", "ASTRYX", "HELIOR", "TAVIRA")
BOT_CASH_PAISE = 50_000_000
BOT_SHARES = 1_000


def account_kinds() -> dict[str, int]:
    out = subprocess.run(  # noqa: S603 (fixed command)
        [
            *COMPOSE,
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "postgres",
            "-d",
            "portfolio",
            "-tAc",
            "SELECT kind, count(*) FROM accounts GROUP BY kind ORDER BY kind",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return {kind: int(n) for kind, n in (line.split("|") for line in out.split())}


def main() -> int:
    wait_until_ready()
    problems: list[str] = []
    for ticker in TICKERS:
        book = http("GET", f"{ENGINE}/internal/v1/books/{ticker}")
        if book["bids"] or book["asks"] or book["sequence"] != 0:
            problems.append(f"{ticker}: book not empty (sequence {book['sequence']})")

    kinds = account_kinds()
    if kinds != {"BOT": 20, "LOAD": 10}:
        problems.append(f"accounts are {kinds}, expected 20 BOT and 10 LOAD only")

    for n in range(1, 21):
        name = f"bot-{n:02d}"
        p = bot(name).portfolio()
        shares = {h["ticker"]: h["quantity"] for h in p["holdings"]}
        if (
            p["cash_paise"] != BOT_CASH_PAISE
            or p["reserved_cash_paise"] != 0
            or shares != dict.fromkeys(TICKERS, BOT_SHARES)
            or any(h["reserved_quantity"] for h in p["holdings"])
        ):
            problems.append(f"{name}: not at its starting balance")

    if problems:
        print("demo-reset: the stack is NOT in the starting state:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(
        "demo-reset: clean start. 5 empty books (sequence 0); 20 bots with ₹5,00,000 and "
        "1,000 shares of each ticker; 10 load accounts; no human accounts."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
