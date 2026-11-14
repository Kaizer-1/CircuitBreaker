#!/usr/bin/env python3
"""Checks that need the Kubernetes deployment (make k8s-up), one subcommand each:

    smoke    make test-k8s: every pod ready, demo trade, bots trade for 2 minutes, the business
             dashboard's trades/s > 0, one order followed across services in Loki
    netpol   connections the NetworkPolicies forbid really fail (and allowed ones work)
    kill     delete an order-service pod while orders flow: replaced, nothing lost
    scale    more order-service replicas = more capacity (no autoscaler: ADR-0007)
    alerts   trigger each Phase 7 alert's condition for real and time how fast it fires
    url      the join QR code's URL survives a restart and k8s-bots, changes only when asked
             (PUBLIC_BASE_URL with k8s-up, or k8s-set-url), and no data is lost
    profile  where a placement's time goes under load, per replica count (a measurement, not a
             pass/fail check): every stage of A-05, Cassandra and Postgres waits, event-loop lag,
             CPU and throttling, Cassandra's GC pauses; saved to build/profile/<label>.json

Standard library only. The environment is prod unless --env staging. These checks change the
running system on purpose (scale, stop, patch); each one puts it back as it found it.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

PROMETHEUS = "http://localhost:9090"
GRAFANA = "http://localhost:3000"
GREEN, RED, BOLD, DIM, RESET = "\033[32m", "\033[31m", "\033[1m", "\033[2m", "\033[0m"
PLATFORM_NAMESPACES = ("traefik", "observability")
# Inside the market-data container (not on this machine): the allocator stops when this file exists.
STOP_LEAK = "/tmp/stop-leak"  # noqa: S108


def kubectl(*args: str, check: bool = True, input_text: str | None = None) -> str:
    out = subprocess.run(  # noqa: S603 (fixed command)
        ["kubectl", "--context", "kind-circuitbreaker", *args],
        capture_output=True,
        text=True,
        input=input_text,
    )
    if check and out.returncode != 0:
        sys.exit(f"kubectl {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout


def get_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=15) as response:  # noqa: S310 (local http:// URLs)
        return json.load(response)


def promql(query: str, at: float | None = None) -> list[dict[str, Any]]:
    params = {"query": query}
    if at is not None:
        params["time"] = f"{at:.3f}"
    result: list[dict[str, Any]] = get_json(
        f"{PROMETHEUS}/api/v1/query?{urllib.parse.urlencode(params)}"
    )["data"]["result"]
    return result


def scalar(query: str, at: float | None = None) -> float | None:
    result = promql(query, at)
    return float(result[0]["value"][1]) if result else None


def verdict(ok: bool, text: str) -> bool:
    print(f"  {GREEN + 'PASS' if ok else RED + 'FAIL'}{RESET} {text}", flush=True)
    return ok


def heading(text: str) -> None:
    print(f"\n{BOLD}{text}{RESET}", flush=True)


def gateway(env: str) -> str:
    return "http://localhost" if env == "prod" else "http://localhost:8090"


def k8s_script(env: str, *args: str) -> subprocess.CompletedProcess[str]:
    """scripts/k8s.py (run <script>, bots, ...) against this environment."""
    return subprocess.run(  # noqa: S603 (fixed command)
        [sys.executable, str(ROOT / "scripts" / "k8s.py"), *args[:1], "--env", env, *args[1:]],
        capture_output=True,
        text=True,
        check=False,
    )


def scale(env: str, workload: str, replicas: int, wait: bool = True) -> None:
    kubectl("scale", "-n", env, workload, f"--replicas={replicas}")
    if wait and replicas > 0:
        kubectl("rollout", "status", "-n", env, workload, "--timeout=300s")


def pods_ready(namespace: str) -> list[str]:
    """Names of pods not ready (completed Job pods are fine)."""
    pods = json.loads(kubectl("get", "pods", "-n", namespace, "-o", "json"))["items"]
    waiting = []
    for pod in pods:
        if pod["status"].get("phase") == "Succeeded":
            continue
        conditions = pod["status"].get("conditions", [])
        if not any(c["type"] == "Ready" and c["status"] == "True" for c in conditions):
            waiting.append(pod["metadata"]["name"])
    return waiting


def bots_on(env: str) -> None:
    if (scalar(f'max(bots_active{{namespace="{env}"}})') or 0) < 20:
        result = k8s_script(env, "bots")
        if result.returncode != 0:
            sys.exit(f"could not start the bots: {result.stderr}")
        time.sleep(30)


# --------------------------------------------------------------------------- url (PUBLIC_BASE_URL)


def check_url(args: argparse.Namespace) -> bool:
    """Restart the cluster with a new PUBLIC_BASE_URL, then do what a presenter does next (bots on,
    k8s-up again without the variable, k8s-set-url) and read the URL the web pod really serves
    (/config.js) after each step. A test account's portfolio must be the same after the restart."""
    import k8s

    env = args.env
    heading(f"The join QR code's URL across restarts ({env})")
    original = k8s.served_url(env)
    bots_were_on = bots_enabled(env)
    print(f"  now: {original}; bots {'on' if bots_were_on else 'off'}")
    session = k8s_portfolio_session(env)
    before = portfolio_of(env, session)
    first, second = "http://10.9.8.7", "http://10.9.8.8:8000"

    def script(*args: str, url: str | None = None) -> None:
        extra = {"PUBLIC_BASE_URL": url} if url else {}
        result = subprocess.run(  # noqa: S603 (fixed command)
            [sys.executable, str(ROOT / "scripts" / "k8s.py"), *args],
            capture_output=True,
            text=True,
            check=False,
            env={**{k: v for k, v in os.environ.items() if k != "PUBLIC_BASE_URL"}, **extra},
        )
        if result.returncode != 0:
            sys.exit(f"k8s.py {' '.join(args)} failed:\n{result.stdout[-2000:]}{result.stderr}")

    ok = True
    try:
        script("down")
        script("up", "--env", env, "--skip-images", url=first)
        ok &= verdict(k8s.served_url(env) == first, f"stop, PUBLIC_BASE_URL={first} k8s-up: served")
        ok &= verdict(portfolio_of(env, session) == before, "test account unchanged (data kept)")
        script("bots", "--env", env)
        ok &= verdict(k8s.served_url(env) == first, "k8s-bots keeps it")
        script("up", "--env", env, "--skip-images")
        ok &= verdict(k8s.served_url(env) == first, "k8s-up without PUBLIC_BASE_URL keeps it")
        script("bots", "--env", env)
        script("set-url", "--env", env, second)
        ok &= verdict(
            k8s.served_url(env) == second and bots_enabled(env),
            f"k8s-set-url {second}: served, the bots still on",
        )
    finally:
        if original:
            script("set-url", "--env", env, original)
        script("bots", "--env", env, *([] if bots_were_on else ["--stop"]))
        print(f"  put back: {k8s.served_url(env)}; bots {'on' if bots_were_on else 'off'}")
    return ok


def bots_enabled(env: str) -> bool:
    """Whether the release was last deployed with the bots on (make k8s-bots)."""
    values = json.loads(
        subprocess.run(  # noqa: S603 (fixed command)
            ["helm", "get", "values", "circuitbreaker", "--kube-context", "kind-circuitbreaker"]
            + ["-n", env, "-o", "json"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    return bool(values.get("bot-traders", {}).get("env", {}).get("BOTS_ENABLED") == "true")


def k8s_portfolio_session(env: str) -> str:
    """A test account (load-13, never ranked) to compare before and after."""
    import stack

    stack.GATEWAY = gateway(env)
    return str(stack.tester(13).token)


def portfolio_of(env: str, token: str) -> tuple[int, list[tuple[str, int]]]:
    request = urllib.request.Request(  # noqa: S310 (local http:// URL)
        f"{gateway(env)}/v1/portfolio", headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
        body = json.load(response)
    return body["cash_paise"], sorted((h["ticker"], h["quantity"]) for h in body["holdings"])


# --------------------------------------------------------------------------- smoke (make test-k8s)


def check_smoke(args: argparse.Namespace) -> bool:
    import stack

    stack.GATEWAY = gateway(args.env)
    heading(f"Smoke test of the Kubernetes deployment ({args.env})")
    waiting = [p for ns in (*PLATFORM_NAMESPACES, args.env) for p in pods_ready(ns)]
    ok = verdict(not waiting, f"every pod ready{': ' + ', '.join(waiting) if waiting else ''}")

    demo = k8s_script(args.env, "run", "demo_trade.py")
    ok &= verdict(
        demo.returncode == 0, "make k8s-demo-trade: two trades settle with exact balances"
    )
    if demo.returncode != 0:
        print(demo.stdout[-2000:], demo.stderr[-2000:])

    bots = k8s_script(args.env, "bots")
    ok &= verdict(bots.returncode == 0, "the 20 bots start trading")
    print(
        f"  {DIM}bots trade for {args.minutes:g} minutes (scripts/bots_check.py checks them){RESET}"
    )
    check = k8s_script(args.env, "run", "bots_check.py", str(int(args.minutes * 60)))
    ok &= verdict(
        check.returncode == 0, "bots_check: trade rate, two-sided books, no 5xx, no refusals"
    )
    if check.returncode != 0:
        print(check.stdout[-3000:])

    # The business dashboard's own Trades/s query: the panel the audience sees is what is checked.
    dashboard = json.loads((ROOT / "observability" / "dashboards" / "business.json").read_text())
    panel = next(p for p in dashboard["panels"] if p["title"] == "Trades / s")
    trades = scalar(panel["targets"][0]["expr"].replace("$namespace", args.env)) or 0.0
    ok &= verdict(trades > 0, f"Grafana business dashboard: trades/s = {trades:.2f}")

    found = get_json(f"{GRAFANA}/api/search?tag=circuitbreaker")
    titles = sorted(d["title"] for d in found)
    ok &= verdict(len(titles) == 3, f"Grafana has the dashboards: {', '.join(titles)}")

    # Follow one order across services by its correlation ID (Loki).
    trader = stack.tester(33)
    request = urllib.request.Request(  # noqa: S310 (local http:// URL)
        f"{stack.GATEWAY}/v1/orders",
        method="POST",
        data=json.dumps(
            {"ticker": "CALVERA", "side": "BUY", "order_type": "MARKET", "quantity": 1}
        ).encode(),
        headers={"Content-Type": "application/json", **trader.auth},
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        correlation_id = response.headers["X-Correlation-Id"]
    apps: set[str] = set()
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and len(apps) < 4:
        time.sleep(5)
        query = urllib.parse.urlencode(
            {
                "query": f'{{namespace="{args.env}"}} | correlation_id="{correlation_id}"',
                "start": str(int((time.time() - 300) * 1e9)),
                "limit": "500",
            }
        )
        streams = get_json(
            f"{GRAFANA}/api/datasources/proxy/uid/loki/loki/api/v1/query_range?{query}"
        )["data"]["result"]
        apps = {s["stream"]["app"] for s in streams}
    ok &= verdict(
        {"api-gateway", "order-service", "portfolio-service", "matching-engine"} <= apps,
        f"Loki follows one order by correlation ID through {', '.join(sorted(apps))}",
    )
    return ok


# --------------------------------------------------------------------------- network policies

# (from deployment, to host, port, should connect)
CONNECTIONS = [
    ("order-service", "cassandra", 9042, True),
    ("portfolio-service", "postgres", 5432, True),
    ("api-gateway", "order-service", 8000, True),
    ("market-data-service", "matching-engine", 8000, True),
    ("order-service", "redpanda", 9092, True),
    ("portfolio-service", "cassandra", 9042, False),  # another service's data store
    ("order-service", "postgres", 5432, False),  # another service's data store
    ("market-data-service", "postgres", 5432, False),
    ("bot-traders", "order-service", 8000, False),  # around the gateway: trusted headers
    ("chaos-service", "matching-engine", 8000, False),
    ("sre-agent", "portfolio-service", 8000, False),
]


def connects(env: str, source: str, host: str, port: int) -> bool:
    code = (
        "import socket,sys\ns=socket.socket(); s.settimeout(3)\n"
        f"try:\n    s.connect(('{host}',{port})); print('OPEN')\n"
        "except Exception as e:\n    print('BLOCKED', type(e).__name__)"
    )
    out = kubectl("exec", "-n", env, f"deployment/{source}", "--", "python", "-c", code)
    return out.strip().startswith("OPEN")


def check_netpol(args: argparse.Namespace) -> bool:
    heading(f"NetworkPolicies are enforced ({args.env})")
    ok = True
    for source, host, port, allowed in CONNECTIONS:
        opened = connects(args.env, source, host, port)
        what = "allowed, connects" if allowed else "forbidden, refused"
        ok &= verdict(opened == allowed, f"{source} -> {host}:{port} ({what})")
    return ok


# --------------------------------------------------------------------------- kill a pod


def check_kill(args: argparse.Namespace) -> bool:
    """Orders flow from 4 test traders; one order-service pod is deleted mid-flow. Kubernetes
    replaces it, and every accepted order is still known, settles, and leaves no money reserved."""
    import stack

    stack.GATEWAY = gateway(args.env)
    heading(f"Delete an order-service pod while orders flow ({args.env})")
    traders = [stack.tester(n) for n in (41, 42, 43, 44)]
    limit = {"CALVERA": 19640}  # 20% under the reference: rests
    accepted: list[tuple[Any, str]] = []
    failures: list[str] = []
    stop = threading.Event()

    def place(trader: Any) -> None:
        while not stop.is_set():
            try:
                order = trader.place("BUY", 1, limit["CALVERA"])
                accepted.append((trader, order["order_id"]))
            except stack.ApiError as exc:
                failures.append(f"HTTP {exc.status}")
            except OSError as exc:
                failures.append(type(exc).__name__)
            time.sleep(0.2)

    threads = [threading.Thread(target=place, args=(t,), daemon=True) for t in traders]
    for t in threads:
        t.start()
    time.sleep(10)
    pod = kubectl(
        "get",
        "pods",
        "-n",
        args.env,
        "-l",
        "app.kubernetes.io/name=order-service",
        "-o",
        "jsonpath={.items[0].metadata.name}",
    )
    killed_at = time.monotonic()
    kubectl("delete", "pod", "-n", args.env, pod, "--wait=false")
    print(f"  {DIM}deleted {pod} with orders in flight{RESET}")
    # Replaced = two ready order-service pods again, neither of them the deleted one.
    replaced = None
    while time.monotonic() - killed_at < 180:
        pods = json.loads(
            kubectl(
                "get",
                "pods",
                "-n",
                args.env,
                "-l",
                "app.kubernetes.io/name=order-service",
                "-o",
                "json",
            )
        )["items"]
        ready = [
            q["metadata"]["name"]
            for q in pods
            if q["metadata"]["name"] != pod
            and not q["metadata"].get("deletionTimestamp")
            and any(
                c["type"] == "Ready" and c["status"] == "True"
                for c in q["status"].get("conditions", [])
            )
        ]
        if len(ready) >= 2:
            replaced = time.monotonic() - killed_at
            break
        time.sleep(1)
    time.sleep(15)
    stop.set()
    for t in threads:
        t.join()

    ok = verdict(
        replaced is not None,
        f"Kubernetes replaced the pod: 2 ready again after {replaced or 0:.0f} s",
    )
    ok &= verdict(
        not failures,
        f"{len(accepted)} orders accepted, {len(failures)} refused while the pod was replaced"
        + (f": {', '.join(sorted(set(failures)))}" if failures else ""),
    )
    for trader, order_id in accepted:
        try:
            trader.cancel(order_id)
        except stack.ApiError as exc:
            if exc.status != 409:  # already finished is fine
                failures.append(f"cancel HTTP {exc.status}")
    deadline = time.monotonic() + 90
    statuses: dict[str, str] = {}
    while time.monotonic() < deadline:
        statuses = {oid: t.order(oid)["status"] for t, oid in accepted}
        if all(s in ("CANCELLED", "FILLED", "REJECTED") for s in statuses.values()):
            break
        time.sleep(3)
    unsettled = [oid for oid, s in statuses.items() if s not in ("CANCELLED", "FILLED", "REJECTED")]
    ok &= verdict(
        not unsettled and len(statuses) == len(accepted),
        f"every accepted order is known and finished ({len(statuses)}; unsettled {len(unsettled)})",
    )
    reserved = sum(int(t.portfolio()["reserved_cash_paise"]) for t in traders)
    ok &= verdict(reserved == 0, f"no money left reserved ({reserved} paise)")
    return ok


# --------------------------------------------------------------------------- scale


def check_scale(args: argparse.Namespace) -> bool:
    """The same load at 2 and at --to order-service replicas: latency high, then back down.

    Kubernetes has no autoscaler here (ADR-0007): a person scales now, the SRE agent from Phase 7.
    Each phase is measured in steady state, after the replicas are ready and settled. The default
    load is the traffic spike as a panic sell (chaos scenarios section 3.3: 90% MARKET sells from
    the bots' accounts), so the bot fleet is stopped while it runs and started again after.
    """
    low, high = 2, args.to
    heading(
        f"Capacity: {args.rate:.0f} req/s ({args.mix}), {low} then {high} order-service replicas"
    )
    post_p95 = (
        "histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket"
        f'{{namespace="{args.env}",service="order-service",route="/v1/orders",method="POST"}}[{{w}}s])))'
    )
    bots_were_on = args.mix == "panic" and bots_enabled(args.env)
    if bots_were_on:
        k8s_script(args.env, "bots", "--stop")
    results = {}
    try:
        for replicas in (low, high):
            scale(args.env, "deployment/order-service", replicas)
            time.sleep(args.settle)
            started = time.time()
            summary = load(args.env, args.rate, args.seconds, args.mix)
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
                f"  {replicas} replicas: POST p95 {client:7.0f} ms client, {srv:7.0f} ms"
                f" order-service  {rate:.0f} req/s"
            )
    finally:
        scale(args.env, "deployment/order-service", 2)  # back to the documented default
        if bots_were_on:
            k8s_script(args.env, "bots")
    slow, fast = results[low][1], results[high][1]
    ok = verdict(
        slow > 500, f"at {low} replicas the load raises order p95 above 500 ms ({slow:.0f} ms)"
    )
    ok &= verdict(
        fast < 500 and fast < slow / 2, f"at {high} replicas it is back down ({fast:.0f} ms)"
    )
    return ok


PROFILE_WINDOW_SKIP = 10  # seconds at the start of a run left out (connections opening)


def profile_queries(env: str) -> dict[str, str]:
    """name -> PromQL with {w} for the window, one number each."""
    ns = f'namespace="{env}"'

    def q(quantile: float, metric: str, labels: str) -> str:
        return (
            f"histogram_quantile({quantile}, sum by (le) (rate({metric}_bucket"
            f"{{{ns},{labels}}}[{{w}}s])))"
        )

    post = 'route="/v1/orders",method="POST"'
    out = {
        "post/s at order-service": (
            f'sum(rate(http_request_duration_seconds_count{{{ns},service="order-service",{post}}}'
            "[{w}s]))"
        ),
    }
    for p in (0.5, 0.95):
        tag = f"p{int(p * 100)}"
        out |= {
            f"gateway POST {tag}": q(
                p, "http_request_duration_seconds", f'service="api-gateway",{post}'
            ),
            f"order-service POST {tag}": q(
                p, "http_request_duration_seconds", f'service="order-service",{post}'
            ),
            **{
                f"stage {stage} {tag}": q(p, "order_place_stage_seconds", f'stage="{stage}"')
                for stage in ("claim", "reserve", "store", "publish")
            },
            f"cassandra thread_wait {tag}": q(p, "cassandra_call_seconds", 'phase="thread_wait"'),
            f"cassandra execute {tag}": q(p, "cassandra_call_seconds", 'phase="execute"'),
            f"portfolio reservation {tag}": q(
                p,
                "http_request_duration_seconds",
                'service="portfolio-service",route="/internal/v1/reservations"',
            ),
            f"postgres pool_wait {tag}": q(p, "postgres_transaction_seconds", 'phase="pool_wait"'),
            f"postgres transaction {tag}": q(
                p, "postgres_transaction_seconds", 'phase="transaction"'
            ),
            **{
                f"loop lag {svc} {tag}": q(p, "event_loop_lag_seconds", f'service="{svc}"')
                for svc in ("api-gateway", "order-service", "portfolio-service")
            },
        }
    for c in (
        "api-gateway",
        "order-service",
        "portfolio-service",
        "postgres",
        "cassandra",
        "redpanda",
        "matching-engine",
    ):
        out[f"cpu {c}"] = (
            f'sum(rate(container_cpu_usage_seconds_total{{{ns},container="{c}"}}[{{w}}s]))'
        )
        out[f"throttled {c}"] = (
            f'sum(rate(container_cpu_cfs_throttled_periods_total{{{ns},container="{c}"}}[{{w}}s]))'
            f' / sum(rate(container_cpu_cfs_periods_total{{{ns},container="{c}"}}[{{w}}s]))'
        )
    return out


def cassandra_pauses(env: str, start: float, end: float) -> dict[str, float]:
    """Cassandra's GC pauses between start and end, from its gc.log files (all rotations)."""
    import re
    from datetime import datetime

    lines = kubectl(
        "-n",
        env,
        "exec",
        "cassandra-0",
        "--",
        "sh",
        "-c",
        r"grep -h '\[info \] GC(.*) Pause' /opt/cassandra/logs/gc.log*",
        check=False,
    )
    pauses = []
    for line in lines.splitlines():
        m = re.match(r"\[(\S+?)\+0000\].* ([\d.]+)ms$", line)
        if m:
            at = datetime.fromisoformat(m[1] + "+00:00").timestamp()
            if start <= at <= end:
                pauses.append(float(m[2]))
    pauses.sort()
    if not pauses:
        return {"gc pauses": 0}

    def pick(p: float) -> float:
        return pauses[min(len(pauses) - 1, int(p * len(pauses)))]

    return {
        "gc pauses": len(pauses),
        "gc pause p50 ms": pick(0.5),
        "gc pause p95 ms": pick(0.95),
        "gc pause max ms": pauses[-1],
        "gc paused % of time": sum(pauses) / 1000 / (end - start) * 100,
    }


def check_profile(args: argparse.Namespace) -> bool:
    """The same load at each replica count; for each, where a placement's time went."""
    heading(f"Profile '{args.label}': {args.rate:.0f} req/s ({args.mix}), replicas {args.replicas}")
    bots_were_on = args.mix == "panic" and bots_enabled(args.env)
    if bots_were_on:
        k8s_script(args.env, "bots", "--stop")
    queries = profile_queries(args.env)
    phases = []
    try:
        for replicas in args.replicas:
            scale(args.env, "deployment/order-service", replicas)
            time.sleep(args.settle)
            started = time.time()
            summary = load(args.env, args.rate, args.seconds, args.mix)
            ended = time.time()
            window = int(ended - started - PROFILE_WINDOW_SKIP)
            row: dict[str, Any] = {
                "replicas": replicas,
                "client rate": summary["rate_median"],
                "client POST p95": summary["post_p95_ms_median"] / 1000,
            }
            for name, query in queries.items():
                row[name] = scalar(query.replace("{w}", str(window)), ended)
            row |= cassandra_pauses(args.env, started + PROFILE_WINDOW_SKIP, ended)
            phases.append(row)
    finally:
        scale(args.env, "deployment/order-service", 2)
        if bots_were_on:
            k8s_script(args.env, "bots")
    names = list(phases[0])
    print(f"\n  {'':34}" + "".join(f"{p['replicas']:>12} repl" for p in phases))
    for name in names[1:]:
        cells = []
        for p in phases:
            v = p.get(name)
            seconds = any(w in name for w in ("p50", "p95")) and "ms" not in name
            cells.append(
                f"{'-':>17}" if v is None else f"{v * 1000:>14.1f} ms" if seconds else f"{v:>17.2f}"
            )
        print(f"  {name:34}" + "".join(cells))
    out = ROOT / "build" / "profile" / f"{args.label}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {"label": args.label, "rate": args.rate, "mix": args.mix, "phases": phases}, indent=2
        )
    )
    print(f"\n  written: {out.relative_to(ROOT)}")
    return True


def load(env: str, rate: float, seconds: float, mix: str = "orders") -> dict[str, Any]:
    out = subprocess.run(  # noqa: S603 (fixed command)
        [
            sys.executable,
            str(ROOT / "scripts" / "load_orders.py"),
            "--rate",
            str(rate),
            "--seconds",
            str(seconds),
            "--mix",
            mix,
            "--json",
        ],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "GATEWAY": gateway(env)},
    )
    summary: dict[str, Any] = json.loads(out.stdout.strip().splitlines()[-1])
    return summary


# --------------------------------------------------------------------------- alerts


def firing(env: str) -> dict[str, dict[str, Any]]:
    alerts = get_json(f"{PROMETHEUS}/api/v1/alerts")["data"]["alerts"]
    return {
        a["labels"]["alertname"]: a
        for a in alerts
        if a["state"] == "firing" and a["labels"].get("namespace", env) == env
    }


def wait_for(
    env: str,
    names: list[str],
    timeout: float,
    started: float,
    condition: Callable[[], float | None],
) -> dict[str, tuple[float, float | None]]:
    """Seconds from the trigger to firing, and from the condition starting (if known) to firing."""
    seen: dict[str, tuple[float, float | None]] = {}
    condition_at: float | None = None
    while time.monotonic() - started < timeout and len(seen) < len(names):
        now = time.monotonic()
        if condition_at is None:
            condition_at = condition()
        active = firing(env)
        for name in names:
            if name in active and name not in seen:
                since_condition = now - condition_at if condition_at else None
                seen[name] = (now - started, since_condition)
        time.sleep(2)
    return seen


def wait_quiet(env: str, names: list[str], timeout: float = 420) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not set(names) & set(firing(env)):
            return
        time.sleep(5)


def report(names: list[str], seen: dict[str, tuple[float, float | None]], how: str) -> bool:
    ok = True
    for name in names:
        if name not in seen:
            ok &= verdict(False, f"{name} did not fire ({how})")
            continue
        after_trigger, after_condition = seen[name]
        extra = (
            f", {after_condition:.0f} s after its condition" if after_condition is not None else ""
        )
        ok &= verdict(True, f"{name} fired {after_trigger:.0f} s after the trigger{extra} ({how})")
    return ok


def never(_: Any = None) -> None:
    return None


def alert_case(
    env: str,
    title: str,
    names: list[str],
    trigger: Callable[[], None],
    restore: Callable[[], None],
    timeout: float = 240,
    condition: Callable[[], float | None] = never,
) -> bool:
    print(f"  {DIM}{title}{RESET}", flush=True)
    wait_quiet(env, names)
    started = time.monotonic()
    try:
        trigger()
        seen = wait_for(env, names, timeout, started, condition)
    finally:
        restore()
    return report(names, seen, title)


def check_alerts(args: argparse.Namespace) -> bool:
    env = args.env
    heading(f"Phase 7 alerts: trigger each condition, time the alert ({env})")
    bots_on(env)
    ok = True

    def bots_produce(text: str) -> None:
        kubectl(
            "exec",
            "-i",
            "-n",
            env,
            "redpanda-0",
            "--",
            "rpk",
            "topic",
            "produce",
            "candles.v1",
            "-k",
            "CALVERA",
            input_text=text,
        )

    if "dead-letter" in args.only:
        ok &= alert_case(
            env,
            "a malformed message on candles.v1 (bot-traders dead-letters it)",
            ["DeadLetterMessages"],
            lambda: bots_produce("not an event\n"),
            never,
            90,
        )

    if "crash" in args.only:
        crash = (
            '{"spec":{"template":{"spec":{"containers":[{"name":"chaos-service",'
            '"command":["python","-c","import sys; sys.exit(1)"]}]}}}}'
        )
        ok &= alert_case(
            env,
            "chaos-service patched to exit at start (a crash-looping release)",
            ["PodCrashLooping"],
            lambda: kubectl("patch", "deployment/chaos-service", "-n", env, "-p", crash) and None,
            lambda: kubectl("rollout", "undo", "deployment/chaos-service", "-n", env) and None,
            360,
        )

    if "order-errors" in args.only:
        # The bad-deploy condition (order-service failing while orders keep coming) until Phase 7's
        # FAULT_PROFILE exists: it loses write access to the table it writes first. Readiness only
        # reads, so the pods stay ready.
        # The table order placement claims first: nothing is reserved when it fails, and cancels
        # and event processing (other tables) keep working. Permissions are inherited from the
        # keyspace, and the role holds its own grant on every table it created: MODIFY goes at both
        # levels for this table, and the other tables keep their own grants.
        cql = (
            "{0} MODIFY ON KEYSPACE order_service {1} order_service_role; "
            "{0} MODIFY ON TABLE order_service.orders_by_idempotency_key {1} order_service_role;"
        )
        cqlsh = [
            "exec",
            "-n",
            env,
            "cassandra-0",
            "--",
            "cqlsh",
            "-u",
            "cassandra",
            "-p",
            "cassandra",
        ]
        ok &= alert_case(
            env,
            "order-service cannot write new orders while the bots trade (still ready)",
            ["OrderServiceHighErrorRate"],
            lambda: kubectl(*cqlsh, "-e", cql.format("REVOKE", "FROM")) and None,
            lambda: kubectl(*cqlsh, "-e", cql.format("GRANT", "TO")) and None,
        )

    if "portfolio-down" in args.only:
        # The bots stop trading once they cannot read their portfolio, so order-service then gets no
        # requests: its breaker opens and the gateway answers 503s; order-service's own error rate
        # needs order traffic (the case above).
        ok &= alert_case(
            env,
            "portfolio-service scaled to 0 while the bots trade",
            ["OrderServiceCircuitOpen", "GatewayHighErrorRate"],
            lambda: scale(env, "deployment/portfolio-service", 0, wait=False),
            lambda: scale(env, "deployment/portfolio-service", 2),
        )

    if "portfolio-errors" in args.only:
        sql = "{} INSERT ON reservations {} portfolio_service;"
        psql = [
            "exec",
            "-n",
            env,
            "postgres-0",
            "--",
            "psql",
            "-U",
            "postgres",
            "-d",
            "portfolio",
            "-c",
        ]
        ok &= alert_case(
            env,
            "portfolio-service loses INSERT on reservations (a bad migration; still ready)",
            ["PortfolioHighErrorRate"],
            lambda: kubectl(*psql, sql.format("REVOKE", "FROM")) and None,
            lambda: kubectl(*psql, sql.format("GRANT", "TO")) and None,
        )

    if "lag" in args.only:
        flood: list[subprocess.Popen[str]] = []

        def stop_engine() -> None:
            scale(env, "deployment/matching-engine", 0, wait=False)
            flood.append(
                subprocess.Popen(  # noqa: S603 (fixed command)
                    [
                        sys.executable,
                        str(ROOT / "scripts" / "load_orders.py"),
                        "--rate",
                        "100",
                        "--seconds",
                        "120",
                        "--first-account",  # its own accounts: load-61 ... load-70
                        "61",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    env={**os.environ, "GATEWAY": gateway(env)},
                )
            )

        def start_engine() -> None:
            for proc in flood:
                proc.terminate()
                proc.wait()
            scale(env, "deployment/matching-engine", 1)

        broker_lag = f'max(redpanda_kafka_consumer_group_lag_max{{namespace="{env}"}})'
        ok &= alert_case(
            env,
            "matching-engine stopped while 100 orders/s keep arriving (the broker sees the lag)",
            ["ConsumerLagHigh"],
            stop_engine,
            start_engine,
            240,
            condition=lambda: time.monotonic() if (scalar(broker_lag) or 0) > 1000 else None,
        )

    if "load" in args.only:
        runner: list[threading.Thread] = []

        def overload() -> None:
            scale(env, "deployment/order-service", 2)
            thread = threading.Thread(target=load, args=(env, 200, 120), daemon=True)
            thread.start()
            runner.append(thread)

        ok &= alert_case(
            env,
            "200 orders/s at 2 order-service replicas (a traffic spike)",
            ["OrderServiceHighLatency"],
            overload,
            lambda: [t.join() for t in runner] and None,
            150,
        )

    if "leak" in args.only:
        leak = (
            "import os, time\n"
            "limit = int(open('/sys/fs/cgroup/memory.max').read())\n"
            "used = lambda: int(open('/sys/fs/cgroup/memory.current').read())\n"
            "chunks = []\n"
            "for _ in range(240):\n"
            "    if os.path.exists('/tmp/stop-leak'): break\n"
            "    step = 30 if used() < 0.8 * limit else 2\n"
            "    if used() < 0.9 * limit: chunks.append(bytearray(step * 2**20))\n"
            "    time.sleep(5)\n"
        )
        procs: list[subprocess.Popen[str]] = []
        pod = kubectl(
            "get",
            "pods",
            "-n",
            env,
            "-l",
            "app.kubernetes.io/name=market-data-service",
            "-o",
            "jsonpath={.items[0].metadata.name}",
        )
        selector = f'{{namespace="{env}",container="market-data-service"}}'
        ratio = (
            f"max(container_memory_working_set_bytes{selector}"
            f" / container_spec_memory_limit_bytes{selector})"
        )

        def start_leak() -> None:
            kubectl("exec", "-n", env, pod, "--", "rm", "-f", STOP_LEAK, check=False)
            procs.append(
                subprocess.Popen(  # noqa: S603 (fixed command)
                    [
                        "kubectl",
                        "--context",
                        "kind-circuitbreaker",
                        "exec",
                        "-n",
                        env,
                        pod,
                        "--",
                        "python",
                        "-c",
                        leak,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
            )

        def stop_leak() -> None:
            kubectl("exec", "-n", env, pod, "--", "touch", STOP_LEAK, check=False)
            for proc in procs:
                proc.wait(timeout=30)

        ok &= alert_case(
            env,
            "memory climbing in market-data-service (a process in its container allocates)",
            ["MarketDataMemoryGrowth"],
            start_leak,
            stop_leak,
            240,
            condition=lambda: time.monotonic() if (scalar(ratio) or 0) > 0.7 else None,
        )

    if "over-provisioned" in args.only:
        ok &= alert_case(
            env,
            "4 order-service replicas with only the bots trading (slow: 10-minute rule)",
            ["OrderServiceOverProvisioned"],
            lambda: scale(env, "deployment/order-service", 4),
            lambda: scale(env, "deployment/order-service", 2),
            900,
        )
    return ok


CASES = [
    "dead-letter",
    "crash",
    "order-errors",
    "portfolio-down",
    "portfolio-errors",
    "lag",
    "load",
    "leak",
]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--env", choices=["prod", "staging"], default="prod")
    sub = parser.add_subparsers(dest="check", required=True)
    smoke = sub.add_parser("smoke")
    smoke.add_argument("--minutes", type=float, default=2)
    smoke.set_defaults(func=check_smoke)
    sub.add_parser("netpol").set_defaults(func=check_netpol)
    sub.add_parser("kill").set_defaults(func=check_kill)
    scale_ = sub.add_parser("scale")
    scale_.add_argument("--rate", type=float, default=200)
    scale_.add_argument("--seconds", type=float, default=60)
    scale_.add_argument("--settle", type=float, default=30, help="seconds after the rollout")
    scale_.add_argument("--mix", choices=["panic", "orders", "spike"], default="panic")
    scale_.add_argument("--to", type=int, default=5, help="replicas after scaling up (max 5)")
    scale_.set_defaults(func=check_scale)
    alerts = sub.add_parser("alerts")
    alerts.add_argument(
        "--only",
        nargs="+",
        default=CASES,
        choices=[*CASES, "over-provisioned"],
        help="cases to run (default: all but the 10-minute over-provisioned one)",
    )
    alerts.set_defaults(func=check_alerts)
    sub.add_parser("url").set_defaults(func=check_url)
    profile = sub.add_parser("profile")
    profile.add_argument("--label", required=True, help="name of the saved result")
    profile.add_argument("--rate", type=float, default=100)
    profile.add_argument("--seconds", type=float, default=90)
    profile.add_argument("--settle", type=float, default=30, help="seconds after the rollout")
    profile.add_argument("--mix", choices=["panic", "orders", "spike"], default="panic")
    profile.add_argument("--replicas", type=int, nargs="+", default=[2, 5])
    profile.set_defaults(func=check_profile)
    args = parser.parse_args()
    ok = args.func(args)
    print(f"\n{GREEN + 'all passed' if ok else RED + 'FAILED'}{RESET}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
