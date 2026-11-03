# matching-engine

> **Status:** Phase 1 — runnable skeleton. Follows the layout in
> [coding standards §3](../../docs/05-coding-standards.md#3-folder-layout-inside-every-service)
> (Python package `matching_engine`). Every operation in its API spec answers `501 Not Implemented` until its phase;
> `/healthz`, `/readyz` and `/metrics` work.

| | |
|---|---|
| **Owning team** | Trading Team |
| **Owner** | **Kaizer Dewaswala** |
| **Team type** | Stream-aligned team; the matching engine is treated as a complicated subsystem ([team topology](../../docs/04-team-topology.md)) |
| **Bounded context** | Matching (core, complicated) |

## Responsibility

Keeps one in-memory order book per ticker and matches orders by price-time priority (partial fills, MARKET orders limited by the protection price, cancellations). Event-sourced: every result is appended to the Cassandra event store with a per-ticker sequence number before being published. Takes a snapshot every 500 events per ticker. Serves the internal event-store API used for time travel.

## Owned data

Cassandra keyspace `matching_engine`: `market_events`, `book_snapshots`, `processed_commands`, `publish_cursor`. **The market's source of truth.**

## Events

| | |
|---|---|
| **Produces** | `OrderAccepted`, `TradeExecuted`, `OrderCancelled`, `OrderCancelRejected` → `matching.v1`. Local: `BookSnapshotTaken`. |
| **Consumes** | `OrderPlaced`, `OrderCancelRequested` ← `orders.v1`. |

Schemas: [event catalog](../../docs/03-architecture/03-event-catalog.md).

## API

Internal only: Q-14 `GET /internal/v1/events`, Q-15 `GET /internal/v1/snapshots/{ticker}`, Q-16 `GET /internal/v1/books/{ticker}`. Spec: [`api/matching-engine.yaml`](../../api/matching-engine.yaml). Main work (A-08) is event-driven.

Every service also exposes `/healthz`, `/readyz`, `/metrics` ([coding standards §7](../../docs/05-coding-standards.md#7-standard-operational-endpoints)).

## Synchronous dependencies

None.

## Scaling and operations

**Exactly 1 replica** (single writer per ticker). The agent may restart or roll it back but never scale it. Recovery = snapshot + replay.

## Run and test it alone

```bash
cd services/matching-engine
cp .env.example .env                 # local settings (git-ignored)
uv sync                              # create the virtual environment from uv.lock
uv run pytest                        # unit, smoke and contract tests (contract test reads ../../api/)
uv run ruff check . && uv run ruff format --check . && uv run mypy

make infra                           # from the repo root: Redpanda, Cassandra, Postgres and their init jobs
uv run uvicorn matching_engine.main:create_app --factory --reload --port 8002
curl localhost:8002/readyz         # or `make up` from the repo root to run its container
```

**Ready means:**

- Redpanda, and the topic(s) `orders.v1`, `matching.v1` must exist (the init job creates them)
- Cassandra: logs in with its own role `matching_engine_role` and uses keyspace `matching_engine` only

## Viva Notes

**Q: What is price-time priority?**
A: Best price matches first; among orders at the same price, the earliest one matches first.

**Q: Why only one replica?**
A: Two engines on the same book could both match the same resting order. Correctness needs a single writer per ticker.

**Q: How does it recover after a crash?**
A: Load the latest snapshot per ticker, apply later events from Cassandra, re-publish anything after the publish cursor, resume consuming.
