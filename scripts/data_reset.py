#!/usr/bin/env python3
"""HUMAN-ONLY: give one environment a brand-new market, keeping everything else (owner, 2026-11-27).

    CONFIRM=yes make k8s-data-reset env=prod

Run once after the final rehearsal, before presenting (docs/demo-runbook.md). It wipes the
market's data in ONE environment, all of it together (the event store, the read models, the
orders, the portfolio database and the business topics: wiping some but not others would break
invariants such as "the money reserved matches the open orders"), and reseeds the accounts. It
keeps the cluster, Argo CD, the sealed secrets, the images, Prometheus' and Loki's history, and the
SRE agent's action log, incident counter and settings: the agent rebuilds its hourly budget and
cooldowns from that log, so wiping it would be a runtime override of the guardrails (owner's
decision 9). Cassandra's volume is kept for that reason; its tables are truncated one by one.

Services are stopped and started by scaling to 0 and back to the chart's documented counts, never
by a rollout restart: no new ReplicaSet, so the running releases keep their known-good history.

Afterwards it verifies: no money or shares reserved, every order book empty, every bot and load
account at its seed, no humans (the leaderboard is empty), no incidents, and the agent's budget and
cooldowns as they were before. Then the bots start again (the operator setting decides if they
trade).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import k8s  # noqa: E402
import k8s_checks as checks  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TICKERS = ("CALVERA", "KORVIN", "ASTRYX", "HELIOR", "TAVIRA")

# Every store of an environment: what the reset does to it (also the runbook's table).
CASSANDRA_TRUNCATE = {
    "matching_engine": (
        "market_events",
        "book_snapshots_by_hour",
        "snapshot_hours",
        "ticker_days",
        "processed_commands",
        "publish_cursor",
    ),
    "order_service": (
        "orders_by_id",
        "orders_by_trader",
        "open_orders_by_trader",
        "orders_by_idempotency_key",
        "order_events",
        "outbox",
        "outbox_cursor",
        "processed_events",
    ),  # data_migrations is kept: it records that a schema step already ran
    "market_data": ("candles", "trades_by_ticker", "last_prices"),
    "sre_agent": ("incidents", "incidents_by_day"),  # action_log, incident_counter, settings kept
}
POSTGRES_TRUNCATE = (
    "reservations",
    "settlements",
    "holdings",
    "accounts",
    "processed_events",
    "last_prices",
    "chaos_faults",
)  # schema_migrations is kept
BUSINESS_TOPICS = {  # name: (partitions, retention days), as dev/redpanda/create-topics.sh
    "orders.v1": (5, 7),
    "matching.v1": (5, 7),
    "candles.v1": (5, 1),
    "orders.v1.dlq": (1, 7),
    "matching.v1.dlq": (1, 7),
    "candles.v1.dlq": (1, 7),
}  # incidents.v1 and its DLQ are kept (the agent's own events; chaos-service reads them)
BUSINESS_GROUPS = (
    "matching-engine.orders.v1",
    "order-service.matching.v1",
    "portfolio-service.matching.v1",
    "bot-traders.candles.v1",
)
# Stopped while the stores are wiped, started again in this order (bot-traders last, after the
# checks).
STATEFUL = (
    "matching-engine",
    "order-service",
    "portfolio-service",
    "market-data-service",
    "chaos-service",
    "sre-agent",
)
SEED = {"BOT": (50_000_000, 1_000), "LOAD": (10_000_000, 0)}  # paise, shares of each ticker
SEEDED = {"BOT": 20, "LOAD": 10}


def documented_replicas(env: str, service: str) -> int:
    """The chart's count (charts/circuitbreaker/values.yaml), unless the environment sets one."""
    override = ROOT / "deploy" / "envs" / env / f"{service}.yaml"
    if override.exists():
        found = re.search(r"^\s+replicas:\s*(\d+)", override.read_text(), re.MULTILINE)
        if found:
            return int(found.group(1))
    values = (ROOT / "charts" / "circuitbreaker" / "values.yaml").read_text()
    block = re.search(rf"^{re.escape(service)}:\n((?:[ #].*\n|\n)*)", values, re.MULTILINE)
    if block:
        found = re.search(r"^  replicas:\s*(\d+)", block.group(1), re.MULTILINE)
        if found:
            return int(found.group(1))
    raise SystemExit(f"no documented replica count for {service}")


def exec_in(env: str, pod: str, *command: str) -> str:
    return checks.kubectl("exec", "-n", env, pod, "--", *command)


def cql(env: str, statements: str) -> str:
    admin = k8s.secret_value(env, "cassandra-admin", "password")
    return exec_in(env, "cassandra-0", "cqlsh", "-u", "cassandra", "-p", admin, "-e", statements)


def sql(env: str, statement: str) -> str:
    out = exec_in(
        env, "postgres-0", "psql", "-U", "postgres", "-d", "portfolio", "-At", "-c", statement
    )
    return out.strip()


def rpk(env: str, *args: str) -> str:
    return exec_in(env, "redpanda-0", "rpk", *args)


def scale_to(env: str, service: str, replicas: int) -> None:
    checks.kubectl("scale", "-n", env, f"deployment/{service}", f"--replicas={replicas}")


def wait_gone(env: str, services: tuple[str, ...], timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pods = checks.kubectl(
            "get", "pods", "-n", env, "-l", f"app.kubernetes.io/name in ({','.join(services)})",
            "-o", "name",
        ).split()  # fmt: skip
        if not pods:
            return
        time.sleep(2)
    raise SystemExit(f"pods of {services} still running after {timeout:.0f} s")


def wait_ready(env: str, service: str, timeout: float = 240) -> None:
    checks.kubectl(
        "rollout", "status", "-n", env, f"deployment/{service}", f"--timeout={int(timeout)}s"
    )


def budget(env: str) -> dict[str, Any]:
    settings = checks.chaos_call(env, "GET", "/v1/agent/settings")
    return {"at": time.time(), **(settings.get("budget") or {})}


def step(text: str, began: float) -> None:
    print(f"  {checks.DIM}{time.monotonic() - began:5.0f} s{checks.RESET}  {text}", flush=True)


def verify(env: str, before: dict[str, Any]) -> bool:
    ok = True
    reserved = sql(
        env,
        "SELECT coalesce(sum(reserved_cash_paise), 0) FROM accounts;"
        " SELECT coalesce(sum(reserved_quantity), 0) FROM holdings;"
        " SELECT count(*) FROM reservations",
    ).split()
    ok &= checks.verdict(
        reserved == ["0", "0", "0"], f"nothing reserved (cash, shares, rows: {reserved})"
    )
    for kind, (cash, shares) in SEED.items():
        rows = sql(
            env,
            "SELECT count(*), count(*) FILTER"  # noqa: S608 (constants of this script, no input)
            f" (WHERE cash_paise <> {cash} OR reserved_cash_paise <> 0)"
            f" FROM accounts WHERE kind = '{kind}'",
        ).split("|")
        rows = [r.strip() for r in rows]
        wrong_holdings = sql(
            env,
            "SELECT count(*) FROM holdings h JOIN accounts a USING (trader_id)"  # noqa: S608
            f" WHERE a.kind = '{kind}' AND (h.quantity <> {shares} OR h.reserved_quantity <> 0)",
        )
        ok &= checks.verdict(
            int(rows[0]) == SEEDED[kind] and rows[1] == "0" and wrong_holdings == "0",
            f"{rows[0]} {kind.lower()} accounts, every one at its seed (₹{cash // 100:,} cash,"
            f" {shares} shares of each ticker)",
        )
    humans = sql(env, "SELECT count(*) FROM accounts WHERE kind = 'HUMAN'")
    board = checks.chaos_call(env, "GET", "/v1/leaderboard?limit=5")
    ok &= checks.verdict(
        humans == "0" and not board.get("entries"),
        f"no human accounts ({humans}); the leaderboard is empty",
    )
    books = {t: checks.chaos_call(env, "GET", f"/v1/market/{t}/order-book") for t in TICKERS}
    full = [t for t, b in books.items() if b.get("best_bid_paise") or b.get("best_ask_paise")]
    ok &= checks.verdict(not full, f"every order book empty{f' (not: {full})' if full else ''}")
    incidents = checks.chaos_call(env, "GET", "/v1/incidents?limit=30")["items"]
    ok &= checks.verdict(not incidents, f"no incidents ({len(incidents)} listed)")
    # The agent's budget and cooldowns, rebuilt from its action log at start, must be as before:
    # the same actions left (strict: an action ageing out of the hour during the reset would show
    # here too, and the message says so), each cooldown down by the time the reset took.
    after = budget(env)
    elapsed = after["at"] - before["at"]
    was = before.get("cooldown_seconds_by_service") or {}
    now = after.get("cooldown_seconds_by_service") or {}
    expected = {s: c - elapsed for s, c in was.items() if c - elapsed > 10}
    cooldowns_kept = set(expected) <= set(now) and all(
        abs(now[s] - c) <= 10 for s, c in expected.items()
    )
    left = (before.get("actions_left_this_hour"), after.get("actions_left_this_hour"))
    ok &= checks.verdict(
        left[0] is not None and left[0] == left[1] and cooldowns_kept,
        f"the agent's budget as before: {left[1]} actions left (before {left[0]}; an action ageing"
        f" out of the hour meanwhile would also differ), cooldowns {now} (expected about"
        f" { {s: round(c) for s, c in expected.items()} } after {elapsed:.0f} s)",
    )
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--env", choices=["prod", "staging"], required=True)
    args = parser.parse_args()
    env = args.env
    if os.environ.get("CONFIRM") != "yes":
        print(
            f"Refused: this wipes the market data of {env}"
            " (orders, books, portfolios, incidents).\n"
            f"A person runs it, once: CONFIRM=yes make k8s-data-reset env={env}",
            file=sys.stderr,
        )
        return 2
    checks.use_cluster_secrets(env)
    began = time.monotonic()
    checks.heading(f"data reset of {env}: a new market (human-only)")

    undone = checks.chaos_call(env, "POST", "/v1/chaos/reset")
    step(f"chaos reset first: {undone.get('undone') or 'nothing to undo'}", began)
    before = budget(env)
    step(
        f"the agent's budget now: {json.dumps({k: v for k, v in before.items() if k != 'at'})}",
        began,
    )

    services = (*STATEFUL, "bot-traders")
    for service in services:
        scale_to(env, service, 0)
    wait_gone(env, services)
    step("services stopped (scaled to 0; no new ReplicaSet)", began)

    statements = " ".join(
        f"TRUNCATE {keyspace}.{table};"
        for keyspace, tables in CASSANDRA_TRUNCATE.items()
        for table in tables
    )
    cql(env, statements)
    for keyspace in CASSANDRA_TRUNCATE:  # TRUNCATE keeps a snapshot of what it removed: free it
        exec_in(env, "cassandra-0", "nodetool", "clearsnapshot", "--all", "--", keyspace)
    step(f"Cassandra: {sum(len(t) for t in CASSANDRA_TRUNCATE.values())} tables truncated", began)

    sql(env, f"TRUNCATE {', '.join(POSTGRES_TRUNCATE)} RESTART IDENTITY CASCADE")
    step(f"Postgres: {len(POSTGRES_TRUNCATE)} tables truncated", began)

    rpk(env, "topic", "delete", *BUSINESS_TOPICS)
    for group in BUSINESS_GROUPS:
        with contextlib.suppress(SystemExit):  # a group that never committed: nothing to delete
            rpk(env, "group", "delete", group)
    for topic, (partitions, days) in BUSINESS_TOPICS.items():
        rpk(
            env, "topic", "create", topic, "-p", str(partitions), "-r", "1",
            "-c", f"retention.ms={days * 86_400_000}",
        )  # fmt: skip
    step(
        f"Redpanda: {len(BUSINESS_TOPICS)} topics recreated empty, their consumer groups gone",
        began,
    )

    for service in STATEFUL:
        scale_to(env, service, documented_replicas(env, service))
    for service in STATEFUL:
        wait_ready(env, service)
    step("services started at their documented counts; accounts reseeded", began)

    ok = verify(env, before)
    scale_to(env, "bot-traders", documented_replicas(env, "bot-traders"))
    wait_ready(env, "bot-traders")
    total = time.monotonic() - began
    ok &= checks.verdict(total < 300, f"done in {total:.0f} s (target: under 5 minutes)")
    print(f"\n{checks.GREEN + 'all passed' if ok else checks.RED + 'FAILED'}{checks.RESET}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
