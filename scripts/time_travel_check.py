#!/usr/bin/env python3
"""Measure time travel on the running stack (make up, ideally with make bots for a while).

1. Seek: for every ticker, `samples` random moments in the last `minutes`, each fetched through
   the gateway as the presenter (Q-12 GET /v1/time-travel/{ticker}?at=). Prints how long the seeks
   took (p50, p95, slowest) and how many events each rebuild applied after its snapshot (from the
   engine's Q-15, dev host port only). Fails if p95 is 2 s or more: the Phase 4 target for "scrub
   anywhere in the last hour".
2. Proof: rewind every ticker to now. market-data reads the engine's live book (Q-16), rebuilds to
   that exact sequence and compares hashes (`verification`). Fails unless all five match. Works
   while the bots trade: both sides are compared at one sequence.

Usage: python3 scripts/time_travel_check.py [minutes] [samples]   (default 60 and 20)
"""

from __future__ import annotations

import random
import statistics
import sys
import time
import urllib.parse
from datetime import UTC, datetime, timedelta

from stack import ADMIN_PASSCODE, ENGINE, GATEWAY, http

TICKERS = ("CALVERA", "KORVIN", "ASTRYX", "HELIOR", "TAVIRA")
TARGET_SECONDS = 2.0


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def admin() -> dict[str, str]:
    session = http(
        "POST",
        f"{GATEWAY}/v1/admin/sessions",
        {"nickname": "seek_check", "passcode": ADMIN_PASSCODE},
    )
    return {"Authorization": f"Bearer {session['access_token']}"}


def main() -> int:
    minutes = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    samples = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    auth = admin()
    rng = random.Random(4)  # noqa: S311 (a repeatable measurement, not security)
    now = datetime.now(UTC)
    seconds: list[float] = []
    applied: list[int] = []
    for ticker in TICKERS:
        for _ in range(samples):
            at = iso(now - timedelta(seconds=rng.uniform(1, minutes * 60)))
            query = urllib.parse.urlencode({"at": at})
            started = time.perf_counter()
            view = http("GET", f"{GATEWAY}/v1/time-travel/{ticker}?{query}", headers=auth)
            seconds.append(time.perf_counter() - started)
            snapshot = http("GET", f"{ENGINE}/internal/v1/snapshots/{ticker}?{query}")
            applied.append(view["as_of_sequence"] - snapshot["sequence"])
    seconds.sort()
    p95 = seconds[int(0.95 * (len(seconds) - 1))]
    print(f"{len(seconds)} seeks over the last {minutes} min, all five tickers, as the presenter:")
    print(
        f"  p50 {statistics.median(seconds) * 1000:.0f} ms   p95 {p95 * 1000:.0f} ms   "
        f"slowest {seconds[-1] * 1000:.0f} ms   (target p95 < {TARGET_SECONDS:.0f} s)"
    )
    print(
        f"  events applied after the snapshot: median {statistics.median(applied):.0f}, "
        f"most {max(applied)}"
    )
    ok = p95 < TARGET_SECONDS
    print("  OK" if ok else "  TOO SLOW")

    print("Rewind to now (rebuild vs the live engine book, same sequence):")
    for ticker in TICKERS:
        query = urllib.parse.urlencode({"at": iso(datetime.now(UTC))})
        view = http("GET", f"{GATEWAY}/v1/time-travel/{ticker}?{query}", headers=auth)
        check = view["verification"]
        matched = check is not None and check["matches"]
        ok = ok and matched
        print(
            f"  {ticker:9} sequence {view['as_of_sequence']:>6}  "
            f"{view['state_hash'][:12]}…  {'hashes match' if matched else f'NO MATCH: {check}'}"
        )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
