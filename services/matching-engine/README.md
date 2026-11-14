# matching-engine

> **Status:** Phase 2 — built. Matches orders, stores every result in its Cassandra event store, publishes it to
> `matching.v1`, and rebuilds its books from the store on every start. Layout:
> [coding standards §3](../../docs/05-coding-standards.md#3-folder-layout-inside-every-service) (package `matching_engine`).
> **How the matching rules work, with a worked example: [docs/how-matching-works.md](docs/how-matching-works.md).**

| | |
|---|---|
| **Owning team** | Trading Team |
| **Owner** | **Kaizer Dewaswala** |
| **Team type** | Stream-aligned team; the matching engine is treated as a complicated subsystem ([team topology](../../docs/04-team-topology.md)) |
| **Bounded context** | Matching (core, complicated) |

## Responsibility

Keeps one in-memory order book per ticker and matches orders by price-time priority: partial fills, MARKET orders
limited by their protection price, self-trade prevention (cancel newest), cancellations, and fail-closed expiry
requests ([ADR-0017](../../docs/adr/0017-fail-closed-orphan-release.md)). Event sourced
([ADR-0005](../../docs/adr/0005-event-sourcing-for-matching-engine.md)): every result is appended to the Cassandra
event store with a per-ticker sequence number before it is published. Takes a snapshot every 500 events per ticker.
Serves the internal event-store API used for time travel.

## How it works

| Code | Role |
|---|---|
| `domain/book.py` | `OrderBook`: `decide(command)` runs the rules and returns events; `apply(event)` changes the book (replay uses only this); snapshots and `state_hash()` |
| `domain/model.py` | Commands, events, enums, snapshot format; no I/O |
| `application/engine.py` | The command loop: dedupe → decide → append → apply → publish → snapshot; `recover()` rebuilds every book from the store |
| `events/` | Catalog shape of `orders.v1` commands and `matching.v1` events |
| `adapters/cassandra_store.py` | Event store on Cassandra; a command's events and its `command_id` are one **logged batch** |
| `adapters/redpanda.py` | Publishes to `matching.v1`, key = ticker |
| `api/event_store.py` | Q-14, Q-15, Q-16 |
| `main.py` | Start-up: connect → apply schema → rebuild books → publish backlog → consume `orders.v1` |

**On start-up it rebuilds every book purely from its event store:** latest snapshot, then the events after it, then
publishes anything after the publish cursor, and only then starts consuming. `/readyz` reports `books: not ok` until
then. A failed or timed-out append marks the in-memory books as untrusted, and the next command rebuilds them from the
store first (a Cassandra write timeout can still succeed later).

## Owned data

Cassandra keyspace `matching_engine` (schema: [`src/matching_engine/migrations/`](src/matching_engine/migrations/),
applied at start-up): `market_events`, `ticker_days`, `book_snapshots`, `processed_commands`, `publish_cursor`.
**The market's source of truth.** Snapshots also hold the orders finished or expired in the last hour of event time
(with when each closed, and `forget_before`), so cancel rejections and tombstones answer the same after a restart.

**A memory of one hour** (ADR-0017 note). Until 2026-11-14 a book kept every finished order ID for ever: about 250 bytes
each in memory, plus about 280 more while a snapshot is written. After about 600,000 orders of load tests on kind the
engine passed its 256 MiB limit and was killed at every start-up. Now `forget_before` trails the latest event by one
hour (never moving back), an order that closed before it is forgotten, and an `OrderPlaced` whose `placed_at` is
before it is refused with no events, so a late copy of a forgotten order still never trades. Memory is about one hour
of orders: `closed_orders_remembered{ticker}` shows it.

**Batch size:** a command's events go into one logged batch, so the Cassandra batch limit caps one command at about
3,000 events (dev Cassandra: `batch_size_fail_threshold: 2MiB`, set in `dev/cassandra/entrypoint.sh`).

## Events

| | |
|---|---|
| **Produces** | `OrderAccepted`, `TradeExecuted`, `OrderCancelled`, `OrderCancelRejected`, `OrderExpired` → `matching.v1`. Local: `BookSnapshotTaken`. |
| **Consumes** | `OrderPlaced`, `OrderCancelRequested`, `OrderExpiryRequested` ← `orders.v1` (group `matching-engine.orders.v1`). |

Schemas: [event catalog](../../docs/03-architecture/03-event-catalog.md), files in
[`contracts/events/`](../../contracts/events/README.md). `tests/contract/test_events.py` validates every event the
engine produces against them. Invalid commands go to `orders.v1.dlq` after 5 attempts.

## API

Internal only ([`api/matching-engine.yaml`](../../api/matching-engine.yaml)):

| ID | Endpoint | Answers |
|---|---|---|
| Q-14 | `GET /internal/v1/events?ticker=&after_sequence=&until=&limit=` | Stored events in sequence order, paged |
| Q-15 | `GET /internal/v1/snapshots/{ticker}?at=` | The latest snapshot at or before `at` (or an empty book at sequence 0), with `closed` (the orders finished or expired in the last hour, each with `closed_at`) and `forget_before`, so a rebuild elsewhere reproduces `state_hash` |
| Q-16 | `GET /internal/v1/books/{ticker}` | The live book in priority order, with `state_hash` (SHA-256 of the canonical book; the exact form is in the spec, which also states the hash of its own example) |

**Reads stay cheap as history grows.** Event reads carry a CQL `LIMIT` and stop at the page size or the `until` cut;
picking a snapshot reads only each snapshot's `(sequence, as_of)`, then the one state it needs. Rows are parsed in a
worker thread, not on the command loop, in the store's sequence order: an integration test runs eight concurrent Q-14
readers while the engine appends and checks that each sees 1, 2, 3 … with no gap, repeat or swap, and that "snapshot at
or before T plus the events up to T" gives the hash the live book had at T.

The main work (A-08) is event-driven. Every service also exposes `/healthz`, `/readyz`, `/metrics`
([coding standards §7](../../docs/05-coding-standards.md#7-standard-operational-endpoints)).

**Business metrics:** `trades_executed_total{ticker}`, `commands_processed_total{command,outcome}` (outcome
`PROCESSED`, `NOTHING_TO_DO` or `REFUSED`), `closed_orders_remembered{ticker}` (finished orders still in memory: about
one hour of them), plus the standard event and consumer-lag metrics.

## Synchronous dependencies

None.

## Scaling and operations

**Exactly 1 replica** (single writer per ticker). The agent may restart or roll it back but never scale it. Recovery =
snapshot + replay.

## Run and test it alone

```bash
cd services/matching-engine
cp .env.example .env                 # local settings (git-ignored)
uv sync                              # create the virtual environment from uv.lock
uv run pytest                        # unit, property and contract tests; fails below 100% branch coverage of domain/
uv run pytest --no-cov tests/unit/domain/test_matching.py   # one file (skip the coverage gate)
HYPOTHESIS_PROFILE=deep uv run pytest --no-cov tests/property   # 25,000 examples per property
uv run pytest -m integration --no-cov   # Cassandra adapter against a real Cassandra (Docker, testcontainers)
uv run ruff check . && uv run ruff format --check . && uv run mypy

make infra                           # from the repo root: Redpanda, Cassandra, Postgres and their init jobs
uv run uvicorn matching_engine.main:create_app --factory --reload --port 8002
curl localhost:8002/readyz         # or `make up` from the repo root to run its container
curl localhost:8002/internal/v1/books/CALVERA
```

**Ready means:**

- Redpanda, and the topic(s) `orders.v1`, `matching.v1` must exist (the init job creates them)
- Cassandra: logs in with its own role `matching_engine_role` and uses keyspace `matching_engine` only
- `books`: every order book has been rebuilt from the event store
- `consumer`: group `matching-engine.orders.v1` has joined and read to the end of its partitions at least once

## Viva Notes

**Q: What is price-time priority?**
A: Best price matches first; among orders at the same price, the earliest one matches first.

**Q: Why only one replica?**
A: Two engines on the same book could both match the same resting order. Correctness needs a single writer per ticker.

**Q: How does it recover after a crash?**
A: Load the latest snapshot per ticker, apply later events from Cassandra, re-publish anything after the publish cursor,
then resume consuming. Commands that were not committed are redelivered and skipped by `command_id`.

**Q: How do you prove the rebuilt book is the same?**
A: Q-16 returns a SHA-256 `state_hash` of the canonical book. Kill the engine, restart it, compare the hash. Property
tests do the same at random crash points, and check that nothing is lost or published twice in a different order.

**Q: The engine forgets finished orders after an hour. Can a late copy of an old order trade?**
A: No. A copy of a forgotten order was placed before the order closed, so its `placed_at` is before `forget_before`,
and the engine refuses any placement from before `forget_before` without writing an event. Inside the hour the order is
still remembered, so the copy is refused as a known order. A property test plays late and duplicate placements across
the hour's edge, with restarts from snapshots, and checks that none is ever accepted.
