#!/usr/bin/env python3
"""Checks that need the Kubernetes deployment (make k8s-up), one subcommand each:

    scale    more order-service replicas = more capacity (no autoscaler)
    kill     delete an order-service pod while the bots trade: replaced, nothing lost
    netpol   connections the NetworkPolicies forbid really fail
    smoke    make test-k8s: everything ready, demo trade, bots trade for 2 minutes,
             Prometheus sees trades/s > 0
    alerts   trigger each Phase 7 alert's condition, see it fire

Standard library only. The environment is prod unless --env staging.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from typing import Any

ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
PROMETHEUS = "http://localhost:9090"
GREEN, RED, BOLD, RESET = "\033[32m", "\033[31m", "\033[1m", "\033[0m"


def kubectl(*args: str, check: bool = True) -> str:
    out = subprocess.run(  # noqa: S603 (fixed command)
        ["kubectl", "--context", "kind-circuitbreaker", *args], capture_output=True, text=True
    )
    if check and out.returncode != 0:
        sys.exit(f"kubectl {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout


def promql(query: str, at: float | None = None) -> list[dict[str, Any]]:
    params = {"query": query}
    if at is not None:
        params["time"] = f"{at:.3f}"
    url = f"{PROMETHEUS}/api/v1/query?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 (local Prometheus)
        result: list[dict[str, Any]] = json.load(response)["data"]["result"]
    return result


def scalar(query: str, at: float | None = None) -> float | None:
    result = promql(query, at)
    return float(result[0]["value"][1]) if result else None


def verdict(ok: bool, text: str) -> bool:
    print(f"  {GREEN + 'PASS' if ok else RED + 'FAIL'}{RESET} {text}")
    return ok


def scale_to(env: str, replicas: int) -> None:
    kubectl("scale", "-n", env, "deployment/order-service", f"--replicas={replicas}")
    kubectl("rollout", "status", "-n", env, "deployment/order-service", "--timeout=180s")


def load(env: str, rate: float, seconds: float) -> dict[str, Any]:
    port = "" if env == "prod" else ":8090"
    out = subprocess.run(  # noqa: S603 (fixed command)
        [
            sys.executable,
            str(ROOT / "scripts" / "load_orders.py"),
            "--rate",
            str(rate),
            "--seconds",
            str(seconds),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=True,
        env={**__import__("os").environ, "GATEWAY": f"http://localhost{port}"},
    )
    summary: dict[str, Any] = json.loads(out.stdout.strip().splitlines()[-1])
    return summary


# --------------------------------------------------------------------------- scale


def check_scale(args: argparse.Namespace) -> bool:
    """The same load at 2 and at 4 order-service replicas: latency high, then back down.

    Kubernetes has no autoscaler here (ADR-0007): a person scales now, the SRE agent from Phase 7.
    Each phase is measured in steady state, after the replicas are ready and settled.
    """
    print(
        f"{BOLD}Capacity: {args.rate:.0f} req/s of orders, 2 then 4 order-service replicas{RESET}"
    )
    post_p95 = (
        "histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket"
        f'{{namespace="{args.env}",service="order-service",route="/v1/orders",method="POST"}}[{{w}}s])))'
    )
    results = {}
    for replicas in (2, 4):
        scale_to(args.env, replicas)
        time.sleep(args.settle)
        started = time.time()
        summary = load(args.env, args.rate, args.seconds)
        ended = time.time()
        window = int(ended - started - 10)  # skip the first seconds of the run
        server = scalar(post_p95.replace("{w}", str(window)), ended)
        results[replicas] = (
            summary["post_p95_ms_median"],
            (server or 0) * 1000,
            summary["rate_median"],
        )
        client, srv, rate = results[replicas]
        print(
            f"  {replicas} replicas: POST p95 {client:7.0f} ms client, {srv:7.0f} ms order-service"
            f"  {rate:.0f} req/s"
        )
    scale_to(args.env, 2)  # back to the documented default
    s2, s4 = results[2][1], results[4][1]
    ok = verdict(s2 > 500, f"at 2 replicas the load raises order p95 above 500 ms ({s2:.0f} ms)")
    ok &= verdict(s4 < 500 and s4 < s2 / 2, f"at 4 replicas it is back down ({s4:.0f} ms)")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--env", choices=["prod", "staging"], default="prod")
    sub = parser.add_subparsers(dest="check", required=True)
    scale = sub.add_parser("scale")
    scale.add_argument("--rate", type=float, default=200)
    scale.add_argument("--seconds", type=float, default=60)
    scale.add_argument("--settle", type=float, default=30, help="seconds after the rollout")
    scale.set_defaults(func=check_scale)
    args = parser.parse_args()
    return 0 if args.func(args) else 1


if __name__ == "__main__":
    sys.exit(main())
