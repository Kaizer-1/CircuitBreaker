# 02 — Data Ownership

> **Data ownership** means: for every piece of data, exactly **one** service may write it. Every other service
> gets that data either by **asking the owner's API** or by **listening to the owner's events** and keeping its
> own copy. No service ever reads or writes another service's tables.

## 1. Who owns what

| Service | Store | Logical store | Owns (source of truth for) | Gets from others via |
|---|---|---|---|---|
| `api-gateway` | none | — | Nothing persistent. Sessions are self-contained signed JWTs. | `portfolio-service` API (open account) |
| `order-service` | Cassandra | keyspace `order_service` | Orders and their lifecycle status; order events (`OrderPlaced`, `OrderRejected`, `OrderCancelRequested`) | `matching.v1` events (status updates), `portfolio-service` API (reservations) |
| `matching-engine` | Cassandra | keyspace `matching_engine` | **The market event store**: every `OrderAccepted`, `TradeExecuted`, `OrderCancelled`, `OrderCancelRejected`, plus book snapshots | `orders.v1` events |
| `market-data-service` | Cassandra | keyspace `market_data` | Candles, trade history, depth and last-price read models | `matching.v1` events; `matching-engine` API (time travel) |
| `portfolio-service` | Postgres | database `portfolio` | Accounts, cash, holdings, reservations, settlements, leaderboard | `matching.v1` events |
| `bot-traders` | none | — | Bot strategy state (in memory only; rebuilt on restart) | Public API; `candles.v1` events |
| `sre-agent` | Cassandra | keyspace `sre_agent` | Incidents, their timelines, remediations, agent settings | Prometheus, Loki, Kubernetes APIs |
| `chaos-service` | in memory | — | Running/past chaos experiments (ephemeral demo data, also logged to Loki) | `incidents.v1` events |

### Where the physical servers are shared — and why that's still "no shared database"

To keep a laptop able to run everything, there is **one Cassandra cluster** and **one Postgres server** per
environment. Ownership is enforced *logically*:

- **One keyspace per service** in Cassandra; **one database** in Postgres (only portfolio-service uses Postgres).
- **One credential per service.** Each service has its own Cassandra role with permissions on **its keyspace only**
  (`GRANT ALL PERMISSIONS ON KEYSPACE order_service TO order_service_role`). A bug in market-data-service cannot
  read orders even if someone tries.
- **Schema migrations** for a keyspace live in the owning service's repo folder and run only from its deployment.

A shared *server* is an infrastructure cost decision; a shared *database* (shared tables) is an architecture
decision. We share the first and forbid the second. In production you could give heavy services their own
clusters without changing any code.

## 2. Why no shared database?

> **Integration database (anti-pattern):** several services read and write the same tables to "integrate".

| Problem with a shared database | Example in our domain |
|---|---|
| **Schema coupling** — one team's schema change breaks other teams | Renaming `orders.price` to `limit_price_paise` would break portfolio and market data at the same time → lock-step deploy. |
| **No independent deployability** | Every migration needs every team to coordinate a release. |
| **Hidden write paths, broken invariants** | If market-data could write `accounts`, "cash never negative" is no longer protected by one aggregate. |
| **One-size-fits-all technology** | The market event store wants append-only time-ordered writes (Cassandra); balances want ACID transactions (Postgres). |
| **Shared failure** | A heavy time-travel query would slow down order placement if they shared tables and locks. |

## 3. Store choices per service (and why)

| Service | Store | Why this store |
|---|---|---|
| `matching-engine` | Cassandra | Append-only, time-ordered event log, high write rate, read by range of sequence numbers → Cassandra's partition + clustering keys fit perfectly ([ADR-0004](../adr/0004-cassandra-event-store.md)). |
| `order-service` | Cassandra | Query patterns are known and simple ("order by ID", "orders of a trader, newest first"); write-heavy. |
| `market-data-service` | Cassandra | Time-series candles and trades per ticker, read by time range. |
| `sre-agent` | Cassandra | Append-only incident timelines. Reuses an existing store instead of adding a new technology. |
| `portfolio-service` | Postgres | Settlement must update buyer cash, buyer holding, seller cash, seller holding and the reservation **in one transaction**; needs `CHECK (cash_paise >= 0)` constraints and joins for the leaderboard ([ADR-0006](../adr/0006-cqrs-portfolio-read-model.md)). |
| `chaos-service` | memory | Experiments are demo-session data; losing them on restart is acceptable, and everything is also in Loki logs. |

## 4. Table designs

> **Cassandra modelling rule:** design one table *per query*. The **partition key** decides which node stores
> the rows (and must be given in every query); the **clustering key** sorts rows inside a partition.
> Duplicating data across tables is normal in Cassandra.

### Keyspace `matching_engine` (owner: matching-engine)

| Table | Partition key | Clustering key | Answers the query |
|---|---|---|---|
| `market_events` | `(ticker, day)` | `sequence ASC` | "Events for CHAIWALA after sequence N until time T" (replay, time travel, re-publish) |
| `ticker_days` | `ticker` | `day ASC` | "Which day partitions of `market_events` exist, and the first sequence in each" (where a rebuild or a Q-14 page starts) |
| `book_snapshots` | `ticker` | `sequence DESC` | "Latest snapshot at or before time T" |
| `processed_commands` | `command_id` (the `event_id` of the incoming `OrderPlaced`/`OrderCancelRequested`) | — | "Have I already processed this command?" (idempotency) |
| `publish_cursor` | `ticker` | — | "Last sequence successfully published to `matching.v1`" |

`market_events` columns: `sequence`, `event_id`, `event_type`, `occurred_at`, `payload` (the full JSON envelope).
`day` bucketing keeps partitions from growing forever. A command's events, its `ticker_days` row (on a new day) and its
`processed_commands` row are written in **one logged batch**. A snapshot's `state` is the whole book, including every
finished or expired order ID (so cancel rejections and tombstones give the same answer after a restart).

### Keyspace `order_service` (owner: order-service)

| Table | Partition key | Clustering key | Answers the query |
|---|---|---|---|
| `orders_by_id` | `order_id` | — | Q-08 Get one order |
| `orders_by_trader` | `trader_id` | `placed_at DESC, order_id DESC` | Q-07 List my orders, newest first |
| `order_events` | `order_id` | `occurred_at ASC` | Order history; outbox (column `published` boolean) |
| `outbox` | `bucket` (minutes since 1970) | `occurred_at ASC, event_id` | "Which events are not yet published?" for the relay; a row is deleted once published. `order_events` alone cannot answer this: it is partitioned by order |
| `orders_by_idempotency_key` | `(trader_id, idempotency_key)` | — | "Did this trader already send this `Idempotency-Key`?" (24 h TTL, claimed with a lightweight transaction) |
| `processed_events` | `event_id` | — | Idempotent consumption of `matching.v1` |

### Keyspace `market_data` (owner: market-data-service)

| Table | Partition key | Clustering key | Answers the query |
|---|---|---|---|
| `candles` | `(ticker, interval)` | `bucket_start DESC` | Q-03 Candles for a ticker and interval |
| `trades_by_ticker` | `(ticker, day)` | `executed_at DESC, trade_id` | Q-05 Recent trades |
| `last_prices` | `ticker` | — | Q-02 Instruments with last price and today's figures; also the last applied `matching.v1` `sequence` (a repeat at or below it is skipped) |

Order-book depth (Q-04) is kept **in memory** (rebuilt from events on start-up) because it changes on every event
and is only ever needed "now". Each event's rows (its candles, its trade, the new `sequence`) are one logged batch, and
every table can be dropped and rebuilt from the event store ([read side](08-read-side.md)).

### Keyspace `sre_agent` (owner: sre-agent)

| Table | Partition key | Clustering key | Answers the query |
|---|---|---|---|
| `incidents` | `incident_id` | — | Q-18 Get incident |
| `incidents_by_day` | `day` | `opened_at DESC` | Q-17 List incidents |
| `incident_timeline` | `incident_id` | `occurred_at ASC` | Timeline in Q-18; feed history |
| `agent_settings` | `environment` | — | Q-20 Get settings (approval toggle, paused) |
| `action_log` | `service` | `executed_at DESC` | Cooldown checks ("last action on order-service") |

### Database `portfolio` (owner: portfolio-service, Postgres)

| Table | Key | Purpose |
|---|---|---|
| `accounts` | `trader_id` (PK), `nickname` (UNIQUE) | `kind` (`HUMAN`, `BOT`, `LOAD`), `cash_paise`, `reserved_cash_paise`, `CHECK (cash_paise >= reserved_cash_paise AND reserved_cash_paise >= 0)` |
| `holdings` | `(trader_id, ticker)` | `quantity`, `reserved_quantity`, `avg_cost_paise`, `CHECK (quantity >= reserved_quantity AND reserved_quantity >= 0)` |
| `reservations` | `reservation_id`; UNIQUE `order_id` | `side`, `ticker`, `reserved_cash_paise` or `reserved_quantity`, remaining amounts, `status` (`ACTIVE`, `RELEASED`, `CONSUMED`), `confirmed` (`OrderAccepted` seen), `expiry_requested_at` (the fail-closed sweeper asked for an expiry, [ADR-0017](../adr/0017-fail-closed-orphan-release.md)) |
| `settlements` | `(trade_id, trader_id)` | One row per side of each trade, for the trader's history |
| `last_prices` | `ticker` | Copy of last traded price, used for valuation and market-order protection price |
| `processed_events` | `event_id` | Idempotency: inserted in the same transaction as the change it caused |
| `schema_migrations` | `version` | Which migration files have been applied (start-up applies the rest) |

## 5. Duplicated data (on purpose)

| Data | Owner (source of truth) | Copies | How copies stay fresh |
|---|---|---|---|
| Last traded price | `matching-engine` (in `TradeExecuted`) | `market_data.last_prices`, `portfolio.last_prices` | Each consumes `TradeExecuted` |
| Order status | `order-service` | Bots keep their own open-order list in memory | Public API |
| Instrument list | Helm values `global.instruments` | Config of every service | Re-deploy |

Duplication is the price of independence: portfolio-service can value portfolios even if market-data-service is
down (as in the memory-leak scenario).

## 6. How services share data without a shared database

There are three standard ways to give a service data it does not own, while keeping one owner per piece of data.
**Source:** standard microservices patterns, e.g. C. Richardson, *Microservices Patterns* (Manning, 2018) and
microservices.io (Database per service, API Composition, Domain event, Event sourcing, CQRS). These are not cited from
the textbook; its chapter on data is not among those we have read.

| Pattern | How it works | Trade-off | Where CircuitBreaker uses it |
|---|---|---|---|
| **(a) Synchronous API call to the owner** | Ask the owning service's API at the moment you need the data or decision. | Always fresh, but the caller depends on the owner being up, which is why we add timeouts and a circuit breaker. | order-service → portfolio-service `POST /internal/v1/reservations`; portfolio-service → order-service `POST /internal/v1/order-expiries` (background sweeper, [ADR-0017](../adr/0017-fail-closed-orphan-release.md)); api-gateway → portfolio-service `POST /internal/v1/accounts`; market-data-service → matching-engine `/internal/v1/events` and `/internal/v1/snapshots/{ticker}` (time travel). |
| **(b) Published events + local read-only copies** | The owner publishes events; other services keep their own copy of only the fields they need. | Works even when the owner is down, but the copy is briefly stale (eventual consistency) and uses extra storage. | Last price in `market_data.last_prices` and `portfolio.last_prices` (from `TradeExecuted`); order status in order-service (from `matching.v1`); bots' latest candles (from `candles.v1`). |
| **(c) Event sourcing + CQRS read models** | The owner stores every change as an event; read-side services build query-shaped models by replaying and following those events. | The most flexible: new views and time travel can be built from history. It is also the most moving parts. | matching-engine's event store → market-data candles, depth and time travel; portfolio balances and leaderboard ([ES and CQRS](04-event-sourcing-and-cqrs.md)). |

What we **never** do: read or write another service's tables (the *integration database* anti-pattern, [§2](#2-why-no-shared-database)).

## Viva Notes

**Q: You run one Cassandra cluster — isn't that a shared database?**
A: No. The *server* is shared to save laptop resources, but each service has its own keyspace and its own
credentials that can only access that keyspace. No table is read or written by two services. Splitting the cluster
later needs no code change.

**Q: Why does portfolio-service use Postgres and not Cassandra?**
A: Settlement must update two accounts, two holdings and a reservation atomically, with constraints like "cash can
never go negative". That needs ACID transactions and CHECK constraints, which is what relational databases are best at.

**Q: How does market-data-service get the data for time travel if it can't read the event store?**
A: It calls matching-engine's internal API (`/internal/v1/events` and `/internal/v1/snapshots/{ticker}`). The owner
decides how its data is exposed.

**Q: Why design one Cassandra table per query?**
A: Cassandra can only query efficiently by partition key and in clustering order. So we model tables around queries
(e.g. `orders_by_trader`) and accept duplication.

**Q: What is the cost of data duplication and how do you handle it?**
A: Copies can be briefly stale (eventual consistency) and use extra storage. We accept it: copies are updated by
idempotent event consumers, and the owner is always the source of truth.
