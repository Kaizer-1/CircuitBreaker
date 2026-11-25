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
    gitops   Argo CD never undoes the SRE agent: a scale and a restart by hand stay and the
             Application stays Synced; other drift is reported (OutOfSync), not reverted
    gitops-push  commit + push a change to deploy/envs/<env>/web.yaml, time it to Synced and
             Healthy, then git revert it and time the way back (pushes to the branch Argo CD
             follows: a pull request's branch, never main)
    sealed   SealedSecrets: every one decrypted; a secret sealed with the right certificate
             decrypts; one sealed with another key, for another namespace, or renamed does not
    sealed-drill  restore drill: back up, lose the sealing key, see a SealedSecret fail, restore
             the backup, see it decrypt (needs private/sealed-secrets/sealing-key.json)
    offline  the demo without the internet: make k8s-prepull, cut the node off from the internet
             (private addresses still reachable), delete every pod of the environment and the
             platform, see all of them start again from the node's images and a trade settle
    profile  where a placement's time goes under load, per replica count (a measurement, not a
             pass/fail check): every stage of A-05, Cassandra and Postgres waits, event-loop lag,
             CPU and throttling, Cassandra's GC pauses; saved to build/profile/<label>.json

Standard library only. The environment is prod unless --env staging. These checks change the
running system on purpose (scale, stop, patch); each one puts it back as it found it.
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
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
ARGOCD = "argocd"
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


def use_cluster_secrets(env: str) -> None:
    """The environment's own bot secret and admin passcode (sealed, random since Phase 6) for
    scripts/stack.py and load_orders.py, read before either is imported; set ones win."""
    import k8s

    for var, (secret, key) in k8s.SCRIPT_SECRETS.items():
        value = k8s.secret_value(env, secret, key)
        if value and var not in os.environ:
            os.environ[var] = value


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
    """Whether the bots were last turned on (make k8s-bots writes operator-settings)."""
    import k8s

    return k8s.bots_enabled(env)


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
    argocd = (ARGOCD,) if kubectl("get", "namespace", ARGOCD, check=False) else ()
    waiting = [p for ns in (*PLATFORM_NAMESPACES, *argocd, args.env) for p in pods_ready(ns)]
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
    # Phase 7 (ADR-0020): chaos-service reaches the three hooks and never a data store.
    ("chaos-service", "market-data-service", 8000, True),
    ("chaos-service", "portfolio-service", 8000, True),
    ("chaos-service", "bot-traders", 8000, True),
    ("chaos-service", "postgres", 5432, False),
    ("chaos-service", "cassandra", 9042, False),
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
    require_mains_power("this measurement")
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
    require_mains_power("this measurement")
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


# --------------------------------------------------------------------------- gitops (ADR-0014)


def app_status(name: str) -> tuple[str, str, str]:
    """(sync status, health, synced revision) of one Argo CD Application."""
    app = json.loads(kubectl("get", "applications.argoproj.io", "-n", ARGOCD, name, "-o", "json"))
    st = app.get("status", {})
    return (
        st.get("sync", {}).get("status", "?"),
        st.get("health", {}).get("status", "?"),
        st.get("sync", {}).get("revision", ""),
    )


def refresh(name: str) -> None:
    """Make Argo CD compare now (what its 30 s poll does anyway), then let it finish."""
    kubectl(
        "annotate",
        "applications.argoproj.io",
        "-n",
        ARGOCD,
        name,
        "argocd.argoproj.io/refresh=normal",
        "--overwrite",
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        app = json.loads(
            kubectl("get", "applications.argoproj.io", "-n", ARGOCD, name, "-o", "json")
        )
        if "argocd.argoproj.io/refresh" not in app["metadata"].get("annotations", {}):
            return
        time.sleep(1)


def becomes(app: str, sync: str, timeout: float = 60) -> bool:
    """Whether the Application reaches this sync status within the timeout (its status can lag a
    refresh by a few seconds)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if app_status(app)[0] == sync:
            return True
        time.sleep(2)
    return False


def deployment(env: str, name: str) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(kubectl("get", "deployment", name, "-n", env, "-o", "json"))
    return result


def check_gitops(args: argparse.Namespace) -> bool:
    """The agent-safety rules of ADR-0014, on the running cluster: what the SRE agent's
    scale_deployment and restart_deployment change, Argo CD neither reports nor resets; any other
    live change it reports as OutOfSync and leaves alone (self-heal off)."""
    env = args.env
    app = f"order-service-{env}"
    heading(f"Argo CD and the SRE agent's tools ({env})")
    if not kubectl("get", "applications.argoproj.io", "-n", ARGOCD, app, check=False):
        return verdict(False, f"Argo CD does not manage {env} (make platform)")
    refresh(app)
    ok = verdict(app_status(app)[:2] == ("Synced", "Healthy"), f"{app} starts Synced and Healthy")
    before = deployment(env, "order-service")
    replicas = before["spec"]["replicas"]
    wait = args.wait

    # scale_deployment: replicas belong to the agent, not to Git.
    scale(env, "deployment/order-service", replicas + 1)
    print(f"  {DIM}scaled order-service {replicas} -> {replicas + 1}; waiting {wait:g} s{RESET}")
    time.sleep(wait)
    refresh(app)
    now = deployment(env, "order-service")
    ok &= verdict(
        now["spec"]["replicas"] == replicas + 1, "the scale stays (Argo CD does not reset it)"
    )
    ok &= verdict(
        app_status(app)[0] == "Synced", "and the Application stays Synced (replicas ignored)"
    )
    scale(env, "deployment/order-service", replicas)

    # restart_deployment: the restartedAt annotation is ignored too.
    generation = deployment(env, "order-service")["metadata"]["generation"]
    kubectl("rollout", "restart", "-n", env, "deployment/order-service")
    kubectl("rollout", "status", "-n", env, "deployment/order-service", "--timeout=300s")
    print(f"  {DIM}restarted order-service; waiting {wait:g} s{RESET}")
    time.sleep(wait)
    refresh(app)
    now = deployment(env, "order-service")
    annotations = now["spec"]["template"]["metadata"].get("annotations", {})
    ok &= verdict(
        "kubectl.kubernetes.io/restartedAt" in annotations
        and now["metadata"]["generation"] == generation + 1,
        "the restart stays, and Argo CD rolled nothing back (one new generation: the restart's)",
    )
    ok &= verdict(
        app_status(app)[0] == "Synced", "the Application stays Synced (restartedAt ignored)"
    )

    # A change to a field Git owns (what chaos BAD_DEPLOY does to the image) is drift: reported as
    # OutOfSync, never undone by Argo CD, so the SRE agent's rollback is what fixes it. (A field Git
    # does not set, e.g. a new annotation, is no drift at all: Argo CD compares what Git owns.)
    drift(env, "order-service")
    time.sleep(wait)
    refresh(app)
    ok &= verdict(becomes(app, "OutOfSync"), "a Git-owned field changed by hand: OutOfSync")
    ok &= verdict(drifted(env, "order-service"), "and left in place (self-heal off)")
    undo_drift(env, "order-service")
    time.sleep(5)
    refresh(app)
    ok &= verdict(
        becomes(app, "Synced") and app_status(app)[1] == "Healthy",
        "kubectl rollout undo (what rollback_deployment does) brings it back to Synced",
    )
    return ok


DRIFT = "drifted-by-hand"


def drift(env: str, name: str) -> None:
    """Change a pod-template field the chart sets (checksum/config), as a bad deploy changes the
    image: the pods roll, and Git no longer describes the live Deployment."""
    patch = {"spec": {"template": {"metadata": {"annotations": {"checksum/config": DRIFT}}}}}
    kubectl("patch", f"deployment/{name}", "-n", env, "--type=strategic", "-p", json.dumps(patch))
    kubectl("rollout", "status", "-n", env, f"deployment/{name}", "--timeout=300s")


def drifted(env: str, name: str) -> bool:
    annotations = deployment(env, name)["spec"]["template"]["metadata"].get("annotations", {})
    return bool(annotations.get("checksum/config") == DRIFT)


def undo_drift(env: str, name: str) -> None:
    kubectl("rollout", "undo", "-n", env, f"deployment/{name}")
    kubectl("rollout", "status", "-n", env, f"deployment/{name}", "--timeout=300s")


def git(*args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=False
    )  # noqa: S603, S607
    if out.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout.strip()


def wait_synced_to(app: str, sha: str, started: float, timeout: float = 600) -> tuple[float, float]:
    """Seconds from `started` until Argo CD (a) had synced revision sha and (b) was also Healthy."""
    seen = 0.0
    while time.monotonic() - started < timeout:
        sync, health, revision = app_status(app)
        if revision == sha and sync == "Synced":
            seen = seen or time.monotonic() - started
            if health == "Healthy":
                return seen, time.monotonic() - started
        time.sleep(1)
    sys.exit(f"{app} did not reach {sha[:8]} within {timeout:.0f} s: {app_status(app)}")


def check_gitops_push(args: argparse.Namespace) -> bool:
    """A commit's way to the cluster, measured: change web's pod annotation in Git, push, and time
    Argo CD until the Application has synced that commit and web is Healthy again; then git revert
    the commit, push, and time the way back. No make sync: this is the 30 s poll's real delay."""
    env = args.env
    app = f"web-{env}"
    heading(f"Push to {env}: a Git commit reaches the cluster ({app})")
    if git("status", "--porcelain"):
        sys.exit("the working tree must be clean (the check commits and pushes)")
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if branch == "main":
        sys.exit(
            "never on main (changes reach main only through pull requests): point Argo CD at a"
            " test branch first, make platform revision=<branch>"
        )
    target = json.loads(kubectl("get", "applications.argoproj.io", "-n", ARGOCD, app, "-o", "json"))
    if target["spec"]["source"]["targetRevision"] != branch:
        sys.exit(f"Argo CD follows {target['spec']['source']['targetRevision']}, not {branch}")
    values = ROOT / "deploy" / "envs" / env / "web.yaml"
    original = values.read_text()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if "podAnnotations" in original:
        sys.exit(f"{values.name} already has podAnnotations")
    # The file's last block is web's own mapping, so the annotation is appended inside it.
    annotation = f'  podAnnotations:\n    circuitbreaker.io/gitops-check: "{stamp}"\n'
    values.write_text(original.rstrip("\n") + "\n" + annotation)
    # An emergency fix in the cluster (the SRE agent's rollback) must survive an UNRELATED commit:
    # order-service drifts first, then only web's file changes in Git (ADR-0014, ADR-0016).
    other = f"order-service-{env}"
    drift(env, "order-service")
    refresh(other)
    ok = verdict(becomes(other, "OutOfSync"), f"{other} drifted by hand: OutOfSync")
    git("add", str(values))
    git("commit", "-q", "-m", f"chore({env}): GitOps check, a pod annotation on web ({stamp})")
    sha = git("rev-parse", "HEAD")
    started = time.monotonic()
    git("push", "-q", "origin", branch)
    pushed = time.monotonic() - started
    synced, healthy = wait_synced_to(app, sha, started)
    live = deployment(env, "web")["spec"]["template"]["metadata"]["annotations"]
    ok &= verdict(live.get("circuitbreaker.io/gitops-check") == stamp, "the change is live")
    time.sleep(40)  # one more poll: had Argo CD meant to re-sync order-service, it would have
    refresh(other)
    ok &= verdict(
        drifted(env, "order-service") and app_status(other)[0] == "OutOfSync",
        f"the unrelated commit left {other}'s drift alone (no sync of an untouched service)",
    )
    undo_drift(env, "order-service")
    print(
        f"  push {pushed:.1f} s; after it started: Synced {synced:.0f} s, Healthy {healthy:.0f} s"
    )

    git("revert", "--no-edit", sha)
    back = git("rev-parse", "HEAD")
    started = time.monotonic()
    git("push", "-q", "origin", branch)
    synced_back, healthy_back = wait_synced_to(app, back, started)
    live = deployment(env, "web")["spec"]["template"]["metadata"].get("annotations", {})
    ok &= verdict("circuitbreaker.io/gitops-check" not in live, "git revert: the change is gone")
    print(f"  revert: Synced {synced_back:.0f} s, Healthy {healthy_back:.0f} s after the push")
    result = {
        "env": env,
        "commit": sha,
        "revert": back,
        "push_s": round(pushed, 1),
        "synced_s": round(synced),
        "healthy_s": round(healthy),
        "revert_synced_s": round(synced_back),
        "revert_healthy_s": round(healthy_back),
        "at": stamp,
    }
    out = ROOT / "build" / "gitops" / f"push-{stamp.replace(':', '')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n")
    print(f"  {DIM}saved {out.relative_to(ROOT)}{RESET}")
    return ok


# --------------------------------------------------------------------------- sealed (ADR-0019)


def seal(secret: dict[str, Any], cert: Path) -> dict[str, Any]:
    out = subprocess.run(  # noqa: S603 (fixed command)
        ["kubeseal", "--cert", str(cert), "--format", "json", "--scope", "strict"],
        input=json.dumps(secret),
        capture_output=True,
        text=True,
        check=True,
    )
    result: dict[str, Any] = json.loads(out.stdout)
    return result


def test_secret(env: str, name: str, value: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": env},
        "stringData": {"value": value},
    }


def unsealed(env: str, name: str, timeout: float = 30) -> tuple[str | None, str]:
    """(the Secret's value or None, the SealedSecret's Synced message) once the controller acted."""
    import base64

    deadline = time.monotonic() + timeout
    message = ""
    while time.monotonic() < deadline:
        raw = kubectl("get", "sealedsecret", name, "-n", env, "-o", "json", check=False)
        conditions = json.loads(raw).get("status", {}).get("conditions", []) if raw else []
        cond = next((c for c in conditions if c["type"] == "Synced"), None)
        if cond:
            message = cond.get("message", "")
            if cond["status"] == "True":
                data = json.loads(kubectl("get", "secret", name, "-n", env, "-o", "json"))["data"]
                return base64.b64decode(data["value"]).decode(), message
            return None, message
        time.sleep(1)
    return None, message or "no status"


def cleanup_sealed(env: str, *names: str) -> None:
    for name in names:
        kubectl("delete", "sealedsecret", name, "-n", env, "--ignore-not-found", check=False)
        kubectl("delete", "secret", name, "-n", env, "--ignore-not-found", check=False)


def check_sealed(args: argparse.Namespace) -> bool:
    import secrets as random
    import tempfile

    import sealed_secrets

    env = args.env
    heading(f"Sealed Secrets ({env})")
    result = subprocess.run(  # noqa: S603 (fixed command)
        [sys.executable, str(ROOT / "scripts" / "sealed_secrets.py"), "check", "--env", env],
        capture_output=True,
        text=True,
        check=False,
    )
    ok = verdict(
        result.returncode == 0, f"every SealedSecret in Git decrypted: {result.stdout.strip()}"
    )
    cert = sealed_secrets.CERT
    names = (
        "sealed-check-right",
        "sealed-check-wrong-key",
        "sealed-check-other-ns",
        "sealed-check-renamed",
    )
    try:
        value = random.token_urlsafe(16)
        kubectl(
            "apply", "-f", "-", input_text=json.dumps(seal(test_secret(env, names[0], value), cert))
        )
        got, _ = unsealed(env, names[0])
        ok &= verdict(
            got == value, "sealed with the cluster's certificate: decrypts to the same value"
        )

        with tempfile.TemporaryDirectory() as tmp:
            other = Path(tmp) / "other.pem"
            subprocess.run(  # noqa: S603 (fixed command)
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:4096",
                    "-nodes",
                    "-days",
                    "1",
                    "-subj",
                    "/CN=not-the-sealing-key",
                    "-keyout",
                    str(Path(tmp) / "k.pem"),
                    "-out",
                    str(other),
                ],
                capture_output=True,
                check=True,
            )
            wrong = seal(test_secret(env, names[1], value), other)
        kubectl("apply", "-f", "-", input_text=json.dumps(wrong))
        got, message = unsealed(env, names[1])
        ok &= verdict(got is None, f"sealed with another key: refused ({message})")

        elsewhere = "staging" if env == "prod" else "prod"
        moved = seal(test_secret(elsewhere, names[2], value), cert)
        moved["metadata"]["namespace"] = env
        kubectl("apply", "-f", "-", input_text=json.dumps(moved))
        got, message = unsealed(env, names[2])
        ok &= verdict(got is None, f"sealed for {elsewhere}, applied in {env}: refused ({message})")

        renamed = seal(test_secret(env, "some-other-name", value), cert)
        renamed["metadata"]["name"] = names[3]
        kubectl("apply", "-f", "-", input_text=json.dumps(renamed))
        got, message = unsealed(env, names[3])
        ok &= verdict(got is None, f"renamed after sealing: refused ({message})")
    finally:
        cleanup_sealed(env, *names)
    return ok


def check_sealed_drill(args: argparse.Namespace) -> bool:
    """The restore drill (deployment doc, 'Backing up the sealing key'), for real: the controller
    loses its key, a SealedSecret from before can no longer be decrypted, the backup goes back in,
    and it can again. Running Secrets are untouched throughout (the controller only writes them)."""
    import secrets as random

    import sealed_secrets

    env = args.env
    heading("Sealing key: lose it, restore it from the backup")
    backup = sealed_secrets.KEY_BACKUP
    if not backup.exists():
        return verdict(False, f"no backup at {backup.relative_to(ROOT)}: make sealing-key-backup")
    saved = {item["data"]["tls.crt"] for item in json.loads(backup.read_text())["items"]}
    live = {item["data"]["tls.crt"] for item in sealed_secrets.controller_keys()}
    ok = verdict(live <= saved, f"the backup holds every live key ({len(live)} of {len(saved)})")
    if not ok:
        return ok
    name = "sealed-drill"
    value = random.token_urlsafe(16)
    before = seal(test_secret(env, name, value), sealed_secrets.CERT)

    def controller_restart() -> None:
        kubectl("delete", "pod", "-n", "kube-system", "-l", "app.kubernetes.io/name=sealed-secrets")
        kubectl(
            "rollout",
            "status",
            "-n",
            "kube-system",
            "deployment/sealed-secrets-controller",
            "--timeout=120s",
        )

    try:
        label = f"{sealed_secrets.KEY_LABEL}"
        kubectl("delete", "secret", "-n", "kube-system", "-l", label)
        controller_restart()
        fresh = sealed_secrets.controller_keys()
        ok &= verdict(
            bool(fresh) and not {k["data"]["tls.crt"] for k in fresh} & saved,
            "key lost: the controller made a new one",
        )
        kubectl("apply", "-f", "-", input_text=json.dumps(before))
        got, message = unsealed(env, name)
        ok &= verdict(got is None, f"a SealedSecret from before cannot be decrypted ({message})")
        cleanup_sealed(env, name)

        kubectl("delete", "secret", "-n", "kube-system", "-l", label)
        kubectl("apply", "-f", str(backup))
        controller_restart()
        kubectl("apply", "-f", "-", input_text=json.dumps(before))
        got, _ = unsealed(env, name)
        ok &= verdict(got == value, "backup restored: it decrypts again, to the same value")
    finally:
        cleanup_sealed(env, name)
        if {k["data"]["tls.crt"] for k in sealed_secrets.controller_keys()} != saved:
            print(
                f"  {RED}the controller's keys differ from the backup: run "
                f"`python3 scripts/sealed_secrets.py restore` after deleting the new key{RESET}"
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


# --------------------------------------------------------------------------- offline (prepull)

OFFLINE_CHAIN = "CB-OFFLINE"
LOCAL_NETS = {
    "iptables": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8"],
    "ip6tables": ["fc00::/7", "fe80::/10", "::1/128"],
}


def node_sh(*cmd: str, check: bool = True) -> bool:
    import k8s

    result = subprocess.run(  # noqa: S603 (fixed command)
        ["docker", "exec", k8s.NODE, *cmd], capture_output=True, text=True, check=False
    )
    if check and result.returncode != 0:
        sys.exit(f"failed on the node: {' '.join(cmd)}\n{result.stderr.strip()}")
    return result.returncode == 0


def internet(on: bool) -> None:
    """Off: everything the node (and every pod on it) sends to a non-private address is refused,
    as on a hotspot with no uplink. The cluster, the LAN and the local registry stay reachable."""
    for tool, nets in LOCAL_NETS.items():
        if on:
            for hook in ("OUTPUT", "FORWARD"):
                while node_sh(tool, "-D", hook, "-j", OFFLINE_CHAIN, check=False):
                    pass
            node_sh(tool, "-F", OFFLINE_CHAIN, check=False)
            node_sh(tool, "-X", OFFLINE_CHAIN, check=False)
            continue
        node_sh(tool, "-N", OFFLINE_CHAIN)
        for net in nets:
            node_sh(tool, "-A", OFFLINE_CHAIN, "-d", net, "-j", "RETURN")
        node_sh(tool, "-A", OFFLINE_CHAIN, "-j", "REJECT")
        for hook in ("OUTPUT", "FORWARD"):
            node_sh(tool, "-I", hook, "1", "-j", OFFLINE_CHAIN)


def pull_failures(namespaces: list[str], since: float) -> list[str]:
    bad = []
    for ns in namespaces:
        events = json.loads(kubectl("get", "events", "-n", ns, "-o", "json"))["items"]
        for e in events:
            when = e.get("lastTimestamp") or e.get("eventTime") or ""
            if not when:
                continue
            stamp = calendar.timegm(time.strptime(when[:19], "%Y-%m-%dT%H:%M:%S"))
            pulling = "pull" in e.get("message", "").lower()
            failed = e.get("reason") in ("Failed", "ErrImagePull", "BackOff")
            if failed and pulling and stamp >= since:
                bad.append(f"{ns}/{e['involvedObject']['name']}: {e['message'][:120]}")
    return bad


def check_offline(args: argparse.Namespace) -> bool:
    import k8s

    env = args.env
    heading(f"The demo without the internet ({env})")
    prepull = subprocess.run(  # noqa: S603 (fixed command)
        [sys.executable, str(ROOT / "scripts" / "k8s.py"), "prepull"],
        capture_output=True,
        text=True,
        check=False,
    )
    ok = verdict(prepull.returncode == 0, "make k8s-prepull: every image on the node")
    if not ok:
        print(prepull.stdout[-3000:], prepull.stderr[-1000:])
        return ok
    namespaces = [env, *PLATFORM_NAMESPACES, ARGOCD]
    started = time.time()
    try:
        internet(False)
        probe = "docker.io/library/hello-world:latest"
        node_sh("crictl", "rmi", probe, check=False)
        ok &= verdict(
            not node_sh("crictl", "pull", probe, check=False)
            and not node_sh("curl", "-sS", "-m", "5", "https://ghcr.io", check=False),
            "the node is offline (a registry pull and https://ghcr.io both fail)",
        )
        deleted = time.monotonic()
        for ns in namespaces:
            kubectl("delete", "pods", "-n", ns, "--all", "--wait=false")
        kubectl("delete", "pod", "-n", "kube-system", "-l", "app.kubernetes.io/name=sealed-secrets")
        print(
            f"  {DIM}deleted every pod in {', '.join(namespaces)} and the Sealed Secrets "
            f"controller; waiting for them to start again{RESET}"
        )
        time.sleep(10)
        for ns in namespaces:
            k8s.wait_ready(ns, timeout=900)
        waited = time.monotonic() - deleted
        not_ready = [p for ns in namespaces for p in pods_ready(ns)]
        ok &= verdict(
            not not_ready,
            f"every pod started again offline ({waited:.0f} s after they were deleted)"
            + (f": {', '.join(not_ready)}" if not_ready else ""),
        )
        failures = pull_failures([*namespaces, "kube-system"], started)
        ok &= verdict(not failures, "no image pull was needed (no pull errors in the events)")
        for line in failures[:10]:
            print(f"    {line}")
        demo = k8s_script(env, "run", "demo_trade.py")
        ok &= verdict(demo.returncode == 0, "a demo trade settles with exact balances, offline")
        if demo.returncode != 0:
            print(demo.stdout[-2000:], demo.stderr[-2000:])
        sync, health, _ = app_status(f"order-service-{env}")
        print(
            f"  {DIM}Argo CD while offline: order-service-{env} {sync}/{health} (it cannot "
            f"reach GitHub; the running system does not need it){RESET}"
        )
    finally:
        internet(True)
        back = node_sh("curl", "-sS", "-o", "/dev/null", "-m", "10", "https://ghcr.io", check=False)
        print(f"  {DIM}internet back on the node: {'yes' if back else 'NO, check iptables'}{RESET}")
    return ok


# --------------------------------------------------------------------------- the host (runbook 0)


def on_mains_power() -> bool:
    command = ["pmset", "-g", "batt"]  # macOS: "Now drawing from 'AC Power'"
    out = subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603, S607
    return "AC Power" in out.stdout


def require_mains_power(what: str) -> None:
    """Measurements on battery are invalid: macOS slows the CPU (a soak on battery measured order
    p95 615 ms against 281 on mains). Stop instead of measuring (owner's rule, 2026-11-25)."""
    if not on_mains_power():
        sys.exit(
            f"{RED}STOPPED{RESET} before {what}: the laptop is on battery."
            " Plug it in; nothing was measured."
        )


class HostWatch:
    """The Mac's 1-minute load and its power source every 10 s while measurements run, saved next
    to the results (as the calibration did): a busy or unplugged Mac explains a slow run."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.samples: list[tuple[float, float, bool]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.samples.append((time.time(), os.getloadavg()[0], on_mains_power()))
            self._stop.wait(10)

    def __enter__(self) -> HostWatch:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=15)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lines = ["time\tload1\tpower"]
        lines += [
            f"{t:.0f}\t{load:.2f}\t{'AC' if ac else 'battery'}" for t, load, ac in self.samples
        ]
        self.path.write_text("\n".join(lines) + "\n")

    def summary(self, since: float) -> dict[str, Any]:
        loads = sorted(load for t, load, _ in self.samples if t >= since)
        battery = sum(1 for t, _, ac in self.samples if t >= since and not ac)
        median = loads[len(loads) // 2] if loads else os.getloadavg()[0]
        return {
            "host_load_median": round(median, 1),
            "host_load_max": round(max(loads, default=median), 1),
            "battery_samples": battery,
        }


# --------------------------------------------------------------------------- chaos (Phase 7a)

# For each scenario: the alert it must make fire, a PromQL that is true once its condition began (to
# time the alert from the condition, as Phase 5 did), and how long to wait for it.
CHAOS = {
    "bad-deploy": (
        "BAD_DEPLOY",
        ["OrderServiceHighErrorRate"],
        'sum(rate(http_problems_total{{namespace="{env}",service="order-service",'
        'code="INTERNAL_ERROR"}}[30s])) > 0',
        240,
    ),
    "leak": (
        "MEMORY_LEAK",
        ["MarketDataMemoryGrowth"],
        'max(container_memory_working_set_bytes{{namespace="{env}",container="market-data-service"}})'
        ' / max(container_spec_memory_limit_bytes{{namespace="{env}",'
        'container="market-data-service"}}'
        " > 0) > 0.7",
        900,
    ),
    "panic": (
        "TRAFFIC_SPIKE",
        ["OrderServiceHighLatency"],
        "histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket"
        '{{namespace="{env}",service="order-service",route="/v1/orders"}}[30s]))) > 0.5',
        240,
    ),
    "db": (
        "PORTFOLIO_DB_DEGRADED",
        ["OrderServiceCircuitOpen"],
        'max(circuit_breaker_state{{namespace="{env}",dependency="portfolio-service",'
        'state="open"}}) == 1',
        180,
    ),
}
WATCHED = [
    "OrderServiceHighErrorRate",
    "OrderServiceHighLatency",
    "OrderServiceCircuitOpen",
    "MarketDataMemoryGrowth",
    "GatewayHighErrorRate",
    "PortfolioHighErrorRate",
    "PodCrashLooping",
    "ConsumerLagHigh",
]


def chaos_call(env: str, method: str, path: str, body: Any = None) -> Any:
    import stack

    stack.GATEWAY = gateway(env)
    auth = {"Authorization": f"Bearer {stack.admin_token()}"}
    return stack.http(method, f"{stack.GATEWAY}{path}", body, auth)


def normal(env: str) -> tuple[bool, str]:
    """Orders flow without errors or delay, two order-service replicas, market-data well below its
    memory limit: the exchange as it is with no chaos."""
    errors = scalar(
        f'sum(rate(http_requests_total{{namespace="{env}",service="order-service",status=~"5..",'
        f'route="/v1/orders"}}[30s])) / sum(rate(http_requests_total{{namespace="{env}",'
        f'service="order-service",route="/v1/orders"}}[30s]))'
    )
    p95 = scalar(
        "histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket"
        f'{{namespace="{env}",service="order-service",route="/v1/orders"}}[30s])))'
    )
    memory = scalar(
        f'max(container_memory_working_set_bytes{{namespace="{env}",container="market-data-service"}})'
        f' / max(container_spec_memory_limit_bytes{{namespace="{env}",'
        f'container="market-data-service"}}'
        " > 0)"
    )
    replicas = deployment(env, "order-service")["spec"]["replicas"]
    ok = (errors or 0) < 0.01 and (p95 or 0) < 0.5 and (memory or 0) < 0.5 and replicas == 2
    return ok, (
        f"errors {100 * (errors or 0):.1f}%, p95 {1000 * (p95 or 0):.0f} ms, market-data memory"
        f" {100 * (memory or 0):.0f}% of its limit, order-service {replicas} replicas"
    )


def back_to_normal(env: str, timeout: float = 240) -> tuple[float | None, str]:
    """Seconds until no chaos alert fires and the exchange is normal (or None)."""
    started = time.monotonic()
    detail = ""
    while time.monotonic() - started < timeout:
        quiet = not set(WATCHED) & set(firing(env))
        ok, detail = normal(env)
        if quiet and ok:
            return time.monotonic() - started, detail
        time.sleep(5)
    return None, detail


def memory_mib(env: str) -> float:
    return (
        scalar(
            f'max(container_memory_working_set_bytes{{namespace="{env}",'
            f'container="market-data-service"}})'
        )
        or 0
    ) / 2**20


def restart_cures_the_leak(env: str, watch: float = 120) -> bool:
    """After the agent's restart (the fix step): memory must stay flat (the leak lived only in the
    old process)."""
    kubectl("rollout", "status", "-n", env, "deployment/market-data-service", "--timeout=240s")
    time.sleep(45)  # the old pod gone from the metrics (10 s scrapes, 30 s windows)
    samples = []
    end = time.monotonic() + watch
    while time.monotonic() < end:
        samples.append(memory_mib(env))
        time.sleep(10)
    growth = max(samples) - min(samples)
    return verdict(
        growth < 25,
        f"after the restart market-data stays flat for {watch:.0f} s: {min(samples):.0f} to"
        f" {max(samples):.0f} MiB (the leak lived only in the old process)",
    )


# What the SRE agent will do for each scenario (chaos scenarios section 2), applied by hand the
# moment the alert fires, so "click -> recovered" measures the demo's timeline minus the agent's own
# decision time (Phase 7b adds that: detect <= 10 s, mitigate <= 30 s). The database fault has no
# agent fix (it declines); a person fixes it, here with the presenter's reset.
AGENT_FIX = {
    "bad-deploy": "roll order-service back (kubectl rollout undo, what rollback_deployment does)",
    "leak": "restart market-data-service (what restart_deployment does)",
    "panic": "scale order-service 2 -> 5 under the load (what scale_deployment does)",
    "db": "the presenter's reset (the agent declines; a person fixes the database)",
}


def apply_fix(env: str, name: str) -> None:
    if name == "bad-deploy":
        kubectl("rollout", "undo", "-n", env, "deployment/order-service")
    elif name == "leak":
        kubectl("rollout", "restart", "-n", env, "deployment/market-data-service")
    elif name == "panic":
        kubectl("scale", "-n", env, "deployment/order-service", "--replicas=5")
    else:
        chaos_call(env, "POST", "/v1/chaos/reset")


def wait_cleared(env: str, alert: str, timeout: float = 360) -> float | None:
    """Seconds until the alert is no longer firing (its condition false at an evaluation)."""
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if alert not in firing(env):
            return time.monotonic() - started
        time.sleep(2)
    return None


def condition_started(query: str) -> Callable[[], float | None]:
    return lambda: time.monotonic() if promql(query) else None


def check_chaos(args: argparse.Namespace) -> bool:
    """Each Chaos Panel button, --runs times in a row: it makes its alert fire within its documented
    time, reset brings the exchange back to normal (chaos scenarios section 5; Phase 7a). No SRE
    agent acts yet. The bots trade throughout (the error-rate alerts need traffic)."""
    env = args.env
    heading(f"chaos-service may change one Deployment, only in {env} ({ADR_0020})")
    account = f"system:serviceaccount:{env}:chaos-service"
    other = "prod" if env == "staging" else "staging"
    can = [
        ("patch", "deployments/order-service", env, True),
        ("patch", "deployments/order-service", other, False),
        ("patch", "deployments/portfolio-service", env, False),
        ("patch", "statefulsets/postgres", env, False),
        ("patch", "statefulsets/cassandra", env, False),
        ("get", "secrets", env, False),
        ("list", "deployments", env, False),
    ]
    ok = True
    for verb, what, namespace, allowed in can:
        answer = kubectl(
            "auth", "can-i", verb, what, "-n", namespace, "--as", account, check=False
        ).strip()
        ok &= verdict(
            (answer == "yes") == allowed, f"{verb} {what} in {namespace}: {answer} as expected"
        )
    for source, host, port, allowed in CONNECTIONS[-5:]:
        opened = connects(env, source, host, port)
        ok &= verdict(
            opened == allowed, f"{source} -> {host}:{port} ({'allowed' if allowed else 'refused'})"
        )
    status = urllib.request.Request(  # noqa: S310 (local http)
        f"{gateway(env)}/internal/v1/faults",
        data=b"{}",
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(status, timeout=10) as response:  # noqa: S310 (local http)
            code = response.status
    except urllib.error.HTTPError as exc:
        code = exc.code
    ok &= verdict(
        code != 201, f"a hook is not reachable from outside (POST /internal/... -> {code})"
    )

    bots_on(env)
    runs: list[dict[str, Any]] = []
    out = ROOT / "build" / "chaos" / f"{args.label}.json"
    with HostWatch(out.with_suffix(".hostload.tsv")) as host:
        ok &= chaos_runs(args, runs, host)
    out.write_text(json.dumps(runs, indent=2))
    print(f"  {DIM}saved {out.relative_to(ROOT)} and its host load{RESET}")
    return ok


def chaos_runs(args: argparse.Namespace, runs: list[dict[str, Any]], host: HostWatch) -> bool:
    env, ok = args.env, True
    for name in args.only:
        scenario, alerts, condition, timeout = CHAOS[name]
        heading(f"{scenario}: {args.runs} runs in a row ({env})")
        for run in range(1, args.runs + 1):
            wait_quiet(env, WATCHED, timeout=300)
            require_mains_power(f"{scenario} run {run}")
            params = {"rate_mb_per_minute": args.leak_rate} if name == "leak" else None
            body: dict[str, Any] = {"scenario": scenario}
            if params:
                body["parameters"] = params
            started = time.monotonic()
            began = time.time()
            experiment = chaos_call(env, "POST", "/v1/chaos/experiments", body)
            query = condition.format(env=env)
            seen = wait_for(env, alerts, timeout, started, condition_started(query))
            also = sorted(set(firing(env)) & set(WATCHED) - set(alerts))
            ok &= report(alerts, seen, f"run {run}")
            result: dict[str, Any] = {
                "scenario": scenario,
                "run": run,
                "experiment_id": experiment["experiment_id"],
                "started_at": began,
                "fired_after_start_s": seen.get(alerts[0], (None, None))[0],
                "fired_after_condition_s": seen.get(alerts[0], (None, None))[1],
                "also_firing": also,
            }
            if also:
                print(f"    {DIM}also firing: {', '.join(also)}{RESET}")
            if name == "db" and run == 1 and alerts[0] in seen:
                # The point of the scenario: none of the agent's tools clears it.
                kubectl("rollout", "restart", "-n", env, "deployment/portfolio-service")
                kubectl(
                    "rollout", "status", "-n", env, "deployment/portfolio-service", "--timeout=240s"
                )
                time.sleep(30)
                still = alerts[0] in firing(env)
                ok &= verdict(
                    still,
                    "restarting portfolio-service does not clear it (the fault is in its database)",
                )
            if alerts[0] in seen:
                fixed_at = time.monotonic()
                apply_fix(env, name)
                cleared = wait_cleared(env, alerts[0])
                click = (fixed_at - started + cleared) if cleared is not None else None
                ok &= verdict(
                    cleared is not None,
                    f"fix: {AGENT_FIX[name]} at {fixed_at - started:.0f} s; {alerts[0]} cleared"
                    f" {cleared or 0:.0f} s later: click -> recovered {click or 0:.0f} s",
                )
                result |= {
                    "fix_at_s": fixed_at - started,
                    "cleared_after_fix_s": cleared,
                    "click_to_recovered_s": click,
                }
                if name == "leak":
                    ok &= restart_cures_the_leak(env)
            reset_at = time.monotonic()
            undone = chaos_call(env, "POST", "/v1/chaos/reset")
            after, detail = back_to_normal(env)
            ok &= verdict(
                after is not None,
                f"reset: back to normal in {after or 0:.0f} s ({detail};"
                f" undone: {'; '.join(undone['undone']) or 'nothing'})",
            )
            result |= {
                "reset_to_normal_s": after,
                "normal": detail,
                "undone": undone["undone"],
                "whole_run_s": time.monotonic() - started,
                "reset_after_s": reset_at - started,
            }
            result |= host.summary(began)
            print(
                f"    {DIM}host load median {result['host_load_median']}, max"
                f" {result['host_load_max']}{RESET}"
            )
            if result["battery_samples"]:
                sys.exit(
                    f"{RED}STOPPED{RESET}: the laptop went on battery during {scenario} run {run}."
                )
            runs.append(result)
    return ok


ADR_0020 = "ADR-0020"


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
    # The traffic spike's rate, calibrated on the demo laptop (chaos scenarios section 3.3): above
    # what 2 replicas carry, within what 5 carry.
    scale_.add_argument("--rate", type=float, default=110)
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
    gitops = sub.add_parser("gitops")
    gitops.add_argument("--wait", type=float, default=75, help="seconds (> two 30 s polls)")
    gitops.set_defaults(func=check_gitops)
    sub.add_parser("gitops-push").set_defaults(func=check_gitops_push)
    sub.add_parser("sealed").set_defaults(func=check_sealed)
    sub.add_parser("sealed-drill").set_defaults(func=check_sealed_drill)
    sub.add_parser("offline").set_defaults(func=check_offline)
    chaos = sub.add_parser("chaos")
    chaos.add_argument("--runs", type=int, default=5)
    chaos.add_argument("--only", nargs="+", default=list(CHAOS), choices=list(CHAOS))
    chaos.add_argument("--leak-rate", type=int, default=80, help="MB a minute")
    chaos.add_argument("--label", default="chaos", help="build/chaos/<label>.json")
    chaos.set_defaults(func=check_chaos)
    profile = sub.add_parser("profile")
    profile.add_argument("--label", required=True, help="name of the saved result")
    profile.add_argument("--rate", type=float, default=100)
    profile.add_argument("--seconds", type=float, default=90)
    profile.add_argument("--settle", type=float, default=30, help="seconds after the rollout")
    profile.add_argument("--mix", choices=["panic", "orders", "spike"], default="panic")
    profile.add_argument("--replicas", type=int, nargs="+", default=[2, 5])
    profile.set_defaults(func=check_profile)
    args = parser.parse_args()
    use_cluster_secrets(args.env)
    ok = args.func(args)
    print(f"\n{GREEN + 'all passed' if ok else RED + 'FAILED'}{RESET}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
