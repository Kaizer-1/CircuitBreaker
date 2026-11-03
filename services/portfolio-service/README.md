# portfolio-service

> **Status:** Phase 1 — runnable skeleton. Follows the layout in
> [coding standards §3](../../docs/05-coding-standards.md#3-folder-layout-inside-every-service)
> (Python package `portfolio_service`). Every operation in its API spec answers `501 Not Implemented` until its phase;
> `/healthz`, `/readyz` and `/metrics` work.

| | |
|---|---|
| **Owning team** | Market Insights Team |
| **Owner** | **MD Asif Ali** |
| **Team type** | Stream-aligned ([team topology](../../docs/04-team-topology.md)) |
| **Bounded context** | Portfolio and Accounts (supporting) |

## Responsibility

Owns every account's cash and holdings. Opens accounts (humans get ₹1,00,000; bots and load accounts are seeded from config, bots with shares), reserves cash or shares for new orders (enforcing no margin and no short selling), settles trades in one SQL transaction, releases leftover or orphaned reservations, values portfolios at last prices and ranks the leaderboard. A CQRS read model of trades plus the `Account` aggregate.

## Owned data

Postgres database `portfolio`: `accounts`, `holdings`, `reservations`, `settlements`, `last_prices`, `processed_events`.

## Events

| | |
|---|---|
| **Produces** | — on the bus. Local: `AccountOpened`, `ReservationCreated`, `TradeSettled`, `ReservationReleased`. |
| **Consumes** | `OrderAccepted`, `TradeExecuted`, `OrderCancelled` ← `matching.v1`. |

Schemas: [event catalog](../../docs/03-architecture/03-event-catalog.md).

## API

Q-09 `GET /v1/portfolio`, Q-11 `GET /v1/leaderboard`, WebSocket Q-10 `/v1/ws/portfolio`; internal A-02 `POST /internal/v1/accounts`, A-06 `POST /internal/v1/reservations`. Spec: [`api/portfolio-service.yaml`](../../api/portfolio-service.yaml). Settlement (A-09) is event-driven.

Every service also exposes `/healthz`, `/readyz`, `/metrics` ([coding standards §7](../../docs/05-coding-standards.md#7-standard-operational-endpoints)).

## Synchronous dependencies

None.

## Scaling and operations

Default 2 replicas, agent may scale 1–5. Concurrent settlements on the same account are serialised by row locks in Postgres.

## Run and test it alone

```bash
cd services/portfolio-service
cp .env.example .env                 # local settings (git-ignored)
uv sync                              # create the virtual environment from uv.lock
uv run pytest                        # unit, smoke and contract tests (contract test reads ../../api/)
uv run ruff check . && uv run ruff format --check . && uv run mypy

make infra                           # from the repo root: Redpanda, Cassandra, Postgres and their init jobs
uv run uvicorn portfolio_service.main:create_app --factory --reload --port 8004
curl localhost:8004/readyz         # or `make up` from the repo root to run its container
```

**Ready means:**

- Redpanda, and the topic(s) `matching.v1` must exist (the init job creates them)
- Postgres: logs in as `portfolio_service` and uses database `portfolio`

## Viva Notes

**Q: Why Postgres?**
A: Settlement changes several rows that must succeed or fail together, and CHECK constraints guarantee cash never goes negative.

**Q: How is a duplicate TradeExecuted handled?**
A: Its event_id is inserted into processed_events in the same transaction; a duplicate violates the primary key and is skipped.

**Q: What is an orphaned reservation?**
A: Money reserved for an order that never reached the engine (e.g. order-service crashed). It is released if no OrderAccepted arrives within 120 s.
