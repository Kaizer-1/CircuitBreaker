#!/usr/bin/env python3
"""Soak run on Kubernetes: what the outbox relay and the open-orders listing cost over an hour.

    python3 scripts/soak_check.py [--minutes 60] [--rate 40] [--label before]

Needs the cluster (`make k8s-up`) with the bots trading (`make k8s-bots`). While
scripts/load_orders.py places and cancels orders at --rate requests/s, it measures:

- every 5 minutes, Cassandra's own read statistics per table (`nodetool tablestats`: live cells and
  tombstones read per query over the last five minutes). The outbox relay polls every 2 s, so the
  outbox row is "what one relay poll reads";
- every 30 seconds, the time Q-07 `GET /v1/orders?status=open` takes for an account with a long
  history (load-01, which the load keeps placing and cancelling on) and for a bot;
- at the end, order-service's p95 latency for `POST /v1/orders` per 5 minutes and over the whole run
  (Prometheus, server side), plus the load script's own client-side summary.

It prints a table and writes everything to build/soak/<label>.json, so a "before" and an "after"
run can be compared line by line. Standard library only; kubectl and the Prometheus port
(127.0.0.1:9090, infra/kind/cluster.yaml) come from `make k8s-up`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from load_orders import Client, percentile, sign_in  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PROMETHEUS = os.environ.get("PROMETHEUS", "http://127.0.0.1:9090")
TABLES = ("outbox", "orders_by_trader", "open_orders_by_trader")  # the last one only once it exists
SAMPLE_EVERY = 300.0
PROBE_EVERY = 30.0
STAT = re.compile(
    r"^\s*(Average|Maximum) (live cells|tombstones) per slice \(last five minutes\): (\S+)"
)


def tablestats(namespace: str) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for table in TABLES:
        run = subprocess.run(
            [
                "kubectl",
                "exec",
                "-n",
                namespace,
                "cassandra-0",
                "--",
                "nodetool",
                "tablestats",
                f"order_service.{table}",
            ],
            capture_output=True,
            text=True,
        )
        if run.returncode != 0:
            continue  # the table does not exist (yet)
        stats: dict[str, float] = {}
        for line in run.stdout.splitlines():
            match = STAT.match(line)
            if match:
                key = f"{match.group(1).lower()}_{match.group(2).replace(' ', '_')}"
                stats[key] = float("nan") if match.group(3) == "NaN" else float(match.group(3))
        out[table] = stats
    return out


def prometheus(query: str, at: float) -> float | None:
    params = urllib.parse.urlencode({"query": query, "time": f"{at:.3f}"})
    url = f"{PROMETHEUS}/api/v1/query?{params}"
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 (local http:// URL)
        result = json.load(response)["data"]["result"]
    return float(result[0]["value"][1]) if result and result[0]["value"][1] != "NaN" else None


def p95(namespace: str, window: str, at: float) -> float | None:
    """order-service's p95 for POST /v1/orders over `window` ending at `at`, in milliseconds."""
    selector = f'namespace="{namespace}",service="order-service",route="/v1/orders",method="POST"'
    query = (
        "histogram_quantile(0.95, sum by (le) "
        f"(rate(http_request_duration_seconds_bucket{{{selector}}}[{window}])))"
    )
    seconds = prometheus(query, at)
    return None if seconds is None else round(seconds * 1000, 1)


class Probes(threading.Thread):
    """Q-07 status=open every 30 s, timed as the client sees it."""

    def __init__(self, names: list[str], stop: threading.Event) -> None:
        super().__init__(daemon=True)
        self.accounts = sign_in(names)
        self.stop = stop
        self.ms: dict[str, list[float]] = {a.name: [] for a in self.accounts}
        self.errors: dict[str, int] = {a.name: 0 for a in self.accounts}

    def run(self) -> None:
        client = Client()
        while not self.stop.is_set():
            for account in self.accounts:
                began = time.monotonic()
                status, _ = client.call("GET", "/v1/orders?status=open&limit=50", account.token)
                took = (time.monotonic() - began) * 1000
                if status == 200:
                    self.ms[account.name].append(took)
                else:
                    self.errors[account.name] += 1
            self.stop.wait(PROBE_EVERY)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--minutes", type=float, default=60)
    parser.add_argument("--rate", type=float, default=40, help="load: requests per second")
    parser.add_argument("--label", default="run", help="names the JSON file in build/soak/")
    parser.add_argument("--namespace", default="prod")
    args = parser.parse_args()
    seconds = args.minutes * 60

    began = time.time()
    load = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "scripts" / "load_orders.py"),
            "--rate",
            str(args.rate),
            "--seconds",
            str(seconds),
            "--json",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    stop = threading.Event()
    probes = Probes(["load-01", "bot-01"], stop)
    probes.start()
    samples = []
    print(f"soak '{args.label}': {args.minutes:g} min at {args.rate:g} req/s (+ the bots)")
    print(
        f"{'min':>4} {'outbox tomb/read avg':>21} {'max':>7} {'live avg':>9} "
        f"{'by_trader tomb avg':>19} {'open tomb avg':>14} {'POST p95 ms':>12}"
    )
    deadline = began + seconds
    while time.time() < deadline:
        time.sleep(min(SAMPLE_EVERY, max(0.0, deadline - time.time())))
        now = time.time()
        stats = tablestats(args.namespace)
        sample = {
            "minute": round((now - began) / 60, 1),
            "tables": stats,
            "post_p95_ms": p95(args.namespace, "5m", now),
        }
        samples.append(sample)
        outbox = stats.get("outbox", {})
        by_trader = stats.get("orders_by_trader", {})
        open_orders = stats.get("open_orders_by_trader", {})
        print(
            f"{sample['minute']:>4.0f} {outbox.get('average_tombstones', 0):>21.1f} "
            f"{outbox.get('maximum_tombstones', 0):>7.0f} "
            f"{outbox.get('average_live_cells', 0):>9.1f} "
            f"{by_trader.get('average_tombstones', 0):>19.1f} "
            f"{open_orders.get('average_tombstones', float('nan')):>14.1f} "
            f"{sample['post_p95_ms']!s:>12}",
            flush=True,
        )
    stop.set()
    out, _ = load.communicate()
    ended = time.time()
    summary = json.loads(out.strip().splitlines()[-1]) if out.strip() else None
    q07 = {
        name: {
            "p50_ms": round(percentile(ms, 0.5), 1),
            "p95_ms": round(percentile(ms, 0.95), 1),
            "max_ms": round(max(ms), 1) if ms else None,
            "calls": len(ms),
            "errors": probes.errors[name],
        }
        for name, ms in probes.ms.items()
    }
    whole = f"{max(1, int(ended - began))}s"
    result = {
        "label": args.label,
        "minutes": args.minutes,
        "rate": args.rate,
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(began)),
        "samples": samples,
        "post_p95_ms_whole_run": p95(args.namespace, whole, ended),
        "q07_open": q07,
        "load_summary": summary,
    }
    path = ROOT / "build" / "soak" / f"{args.label}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2))
    whole_p95 = result["post_p95_ms_whole_run"]
    print(f"POST /v1/orders p95 over the whole run (server side): {whole_p95} ms")
    for name, row in q07.items():
        print(
            f"Q-07 status=open for {name}: p50 {row['p50_ms']} ms, p95 {row['p95_ms']} ms, "
            f"max {row['max_ms']} ms, {row['errors']} errors"
        )
    print(f"written: {path.relative_to(ROOT)}")
    return 0 if load.returncode == 0 else load.returncode


if __name__ == "__main__":
    sys.exit(main())
