# order-service

> **Status:** Phase 2 — built: validation, the A-06 reservation behind a circuit breaker, the outbox and its relay,
> cancellation, order status from `matching.v1`, and A-19 for fail-closed expiries. Layout:
> [coding standards §3](../../docs/05-coding-standards.md#3-folder-layout-inside-every-service) (package `order_service`).

| | |
|---|---|
| **Owning team** | Trading Team |
| **Owner** | **Kaizer Dewaswala** |
| **Team type** | Stream-aligned ([team topology](../../docs/04-team-topology.md)) |
| **Bounded context** | Order Management (core) |

## Responsibility

Accepts LIMIT and MARKET orders, validates ticker, quantity and the ₹0.05 tick size, reserves cash or shares
synchronously via portfolio-service (no margin, no short selling), stores order events with an outbox, publishes
`OrderPlaced` / `OrderCancelRequested` / `OrderExpiryRequested`, and keeps each order's status current from matching
events.

## How it works

| Code | Role |
|---|---|
| `domain/order.py` | Validation codes and the status rules (`accepted`, `traded`, `cancelled`, `cancel_rejected`, `expired`) |
| `application/orders.py` | A-05, A-07, Q-07, Q-08, A-19 and the `matching.v1` updates |
| `application/outbox_relay.py` | Re-publishes unpublished rows after 5 s; never an `OrderPlaced` older than 60 s (no send starts after 50 s) |
| `application/circuit_breaker.py` | The breaker around A-06 |
| `adapters/cassandra_store.py` | The keyspace; one logged batch per write that must be atomic |
| `adapters/portfolio_http.py` | A-06 over HTTP, 500 ms timeout, through the breaker |

**Placing an order** ([sequence diagram 2](../../docs/01-seeds/03-sequence-diagrams.md#2-place-an-order-j-03-j-04--a-05-a-06)):
validate → reserve (A-06) → store the order **and** its `OrderPlaced` in the outbox in one batch → publish at once →
`202`. If the publish fails, the relay retries after 5 s; if 60 s pass without a publish, the order becomes `REJECTED`
(`PUBLISH_TIMEOUT`) and is never sent ([ADR-0011](../../docs/adr/0011-synchronous-reservation-over-saga.md)). A cancel
or expiry is never published before its order's `OrderPlaced`.

**The Kafka producer has a 10 s delivery timeout and is not idempotent** (Phase 3,
[ADR-0017 note](../../docs/adr/0017-fail-closed-orphan-release.md#note-making-the-60-s-rule-true-phase-3)): an idempotent
aiokafka producer never gives up on a message, so a send that hung while Redpanda was down was delivered whenever it came
back. Now the client drops a message it could not deliver within 10 s, and the relay starts no `OrderPlaced` send after
50 s, so none reaches the broker after 60 s. The engine drops a duplicate send by `event_id`; aiokafka keeps one request
in flight per partition, so order holds. `tests/integration/test_redpanda_publish_deadline.py` proves it on a real
Redpanda that is stopped and started again (`PUBLISH_DELIVERY_TIMEOUT_SECONDS`).

| Answer | When |
|---|---|
| `202` + `Location` | Accepted, status `PENDING` |
| `422 UNKNOWN_TICKER` / `INVALID_TICK_SIZE` / `VALIDATION_FAILED` | The rules; nothing reserved or stored |
| `422 INSUFFICIENT_FUNDS` / `INSUFFICIENT_SHARES` | portfolio-service refused; stored as `REJECTED` (local event `OrderRejected`) |
| `503 DEPENDENCY_UNAVAILABLE` | portfolio-service down or its breaker open; or the order store failed after reserving (that reservation is released later through the fail-closed expiry, [ADR-0017](../../docs/adr/0017-fail-closed-orphan-release.md)) |
| `400 BAD_REQUEST` | The body is not JSON |

**Idempotency-Key** (`POST /v1/orders`): the key is bound to a new `order_id` with a Cassandra lightweight transaction
(`IF NOT EXISTS`, 24 h TTL). A retry returns the original order (or the original `422`). A retry after a crash between
binding the key and storing the order finishes that same order; A-06 is idempotent per `order_id`, so nothing is
reserved twice.

**Two writers, no lost updates.** The API writes only `cancel_requested`; the `matching.v1` consumer writes only the
status columns (and clears `cancel_requested` on `OrderCancelled`, `OrderCancelRejected` or a full fill). Cassandra
keeps the latest value per column, so neither overwrites the other. Each order also remembers the last `sequence`
applied, so replayed events change nothing; `processed_events` drops duplicates.

**A-19** (`POST /internal/v1/order-expiries`, called by portfolio-service's sweeper): writes an `OrderExpiryRequested`
(event ID = UUIDv5 of the order ID) to the outbox, even for an order it never stored. An order still `PENDING` and
never published becomes `REJECTED` (`PUBLISH_TIMEOUT`) first. A repeat answers `202` with the original `requested_at`.

## Circuit breaker (shown in the demo)

> Like the switch in a fuse box: after repeated failures it opens and calls fail at once, instead of every request
> waiting for a dependency that is down.

Around A-06: 500 ms timeout; **5 consecutive failures** (timeout, connection error, 5xx) **open it for 10 s**; then
one trial call: success closes it, failure opens it again. A business answer (`422`) is a success. While it is open,
`POST /v1/orders` answers `503 DEPENDENCY_UNAVAILABLE` immediately.

| Metric | Meaning |
|---|---|
| `circuit_breaker_state{dependency="portfolio-service",state="closed\|open\|half_open"}` | `1` for the current state, `0` for the others |
| `circuit_breaker_trips_total{dependency="portfolio-service"}` | How many times it opened |

```bash
curl -s localhost:8001/metrics | grep ^circuit_breaker
docker compose --profile infra --profile app stop portfolio-service   # then place 5 orders: state="open" 1
```

Other business metrics: `orders_placed_total{order_type,side}`, `orders_rejected_total{reason}`.

**Under load (found in Phase 3 with the 20 bots running).** Two fixes keep order placement fast and error-free:

- The Cassandra driver uses **protocol v4**. With v5, concurrent queries on the asyncio reactor corrupted its segment
  framing (`CrcException`): the driver dropped the connection, and with one Cassandra node that marked the whole
  cluster down for about a second, so every request failed (`NoHostAvailable`). matching-engine and market-data-service
  use v4 too.
- Q-07 with a status filter reads one row per order (`orders_by_trader` holds only IDs). Those reads share **8 slots**
  across all callers, so a few long listings cannot fill the worker threads and delay a placement. Bots therefore list
  their open orders only at start-up; they follow their Q-10 stream for fills.

## Owned data

Cassandra keyspace `order_service` (schema: [`src/order_service/migrations/`](src/order_service/migrations/), applied at
start-up): `orders_by_id`, `orders_by_trader`, `order_events` (with a `published` flag), `outbox` (unpublished rows,
one partition per minute, deleted once published; the relay looks back 15 minutes), `orders_by_idempotency_key`
(24 h TTL), `processed_events`.

## Events

| | |
|---|---|
| **Produces** | `OrderPlaced`, `OrderCancelRequested`, `OrderExpiryRequested` → `orders.v1` (key = ticker). Local: `OrderRejected`. |
| **Consumes** | `OrderAccepted`, `TradeExecuted`, `OrderCancelled`, `OrderCancelRejected`, `OrderExpired` ← `matching.v1` (group `order-service.matching.v1`). |

Schemas: [event catalog](../../docs/03-architecture/03-event-catalog.md). `tests/contract/test_events.py` validates every
event it publishes against [`contracts/events/`](../../contracts/events/README.md).

## API

A-05 `POST /v1/orders`, A-07 `DELETE /v1/orders/{order_id}`, Q-07 `GET /v1/orders`, Q-08 `GET /v1/orders/{order_id}`
(all with `X-Trader-Id` from the gateway; another trader's order is `404`), internal A-19
`POST /internal/v1/order-expiries`. Spec: [`api/order-service.yaml`](../../api/order-service.yaml).

Every service also exposes `/healthz`, `/readyz`, `/metrics` ([coding standards §7](../../docs/05-coding-standards.md#7-standard-operational-endpoints)).

## Synchronous dependencies

`portfolio-service` `POST /internal/v1/reservations` (A-06), 500 ms timeout, circuit breaker.

## Scaling and operations

Default 2 replicas, agent may scale 1–5 (traffic-spike target). Every replica runs the relay; a row may be published
twice, and the engine drops the copy by `event_id`. Concurrency limit 50 in-flight requests per pod (Phase 7).
Chaos target for `BAD_DEPLOY` and `TRAFFIC_SPIKE`.

## Run and test it alone

```bash
cd services/order-service
cp .env.example .env                 # local settings (git-ignored)
uv sync                              # create the virtual environment from uv.lock
uv run pytest                        # unit, smoke and contract tests (contract test reads ../../api/)
uv run pytest -m integration --no-cov   # the Cassandra store on a real Cassandra 5 (Docker, testcontainers)
uv run ruff check . && uv run ruff format --check . && uv run mypy

make infra                           # from the repo root: Redpanda, Cassandra, Postgres and their init jobs
uv run uvicorn order_service.main:create_app --factory --reload --port 8001
curl localhost:8001/readyz         # or `make up` from the repo root to run its container
```

**Ready means:**

- Redpanda, and the topic(s) `orders.v1`, `matching.v1` must exist (the init job creates them)
- Cassandra: logs in with its own role `order_service_role` and uses keyspace `order_service` only
- `consumer`: group `order-service.matching.v1` has joined and read to the end of its partitions at least once

## Viva Notes

**Q: Why does placing an order return 202?**
A: Matching is asynchronous; the order is accepted for processing and its status changes later via events.

**Q: What is the outbox for?**
A: To make "save the order" and "publish OrderPlaced" reliable without a distributed transaction: both go into
Cassandra in one batch, and a relay re-sends anything unpublished after 5 s, but never an `OrderPlaced` older than
60 s (that order becomes `REJECTED`, `PUBLISH_TIMEOUT`).

**Q: How do you stop overspending?**
A: A synchronous reservation in portfolio-service before publishing; it fails with 422 if cash or shares are insufficient.

**Q: What does the circuit breaker protect?**
A: When portfolio-service is down, every order would wait 500 ms and fail. After 5 failures the breaker opens and
answers 503 at once for 10 s, so phones get a fast answer and portfolio-service is not flooded while it recovers. Its
state and trips are Prometheus metrics.
