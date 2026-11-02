# portfolio-service

> **Status:** Phase 0 — design only, no code yet. This folder will follow the layout in
> [coding standards §3](../../docs/05-coding-standards.md#3-folder-layout-inside-every-service)
> (Python package `portfolio_service`).

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

## Viva Notes

**Q: Why Postgres?**
A: Settlement changes several rows that must succeed or fail together, and CHECK constraints guarantee cash never goes negative.

**Q: How is a duplicate TradeExecuted handled?**
A: Its event_id is inserted into processed_events in the same transaction; a duplicate violates the primary key and is skipped.

**Q: What is an orphaned reservation?**
A: Money reserved for an order that never reached the engine (e.g. order-service crashed). It is released if no OrderAccepted arrives within 120 s.
