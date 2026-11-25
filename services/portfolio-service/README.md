# portfolio-service

> **Status:** Phase 4 — accounts, reservations, settlement, the fail-closed orphan sweeper, the live portfolio
> WebSocket (Q-10) and the leaderboard (Q-11) are built. Layout:
> [coding standards §3](../../docs/05-coding-standards.md#3-folder-layout-inside-every-service) (package `portfolio_service`).

| | |
|---|---|
| **Owning team** | Market Insights Team |
| **Owner** | **MD Asif Ali** |
| **Team type** | Stream-aligned ([team topology](../../docs/04-team-topology.md)) |
| **Bounded context** | Portfolio and Accounts (supporting) |

## Responsibility

Owns every account's cash and holdings. Opens accounts (humans get ₹1,00,000; bots and load accounts are seeded at
start-up, bots with shares), reserves cash or shares for new orders (enforcing no margin and no short selling), settles
trades in one SQL transaction, releases leftover reservations, and values portfolios at last prices. Orphaned
reservations are released **fail-closed** ([ADR-0017](../../docs/adr/0017-fail-closed-orphan-release.md)): only after the
matching engine confirms the order can never trade. A CQRS read model of trades plus the `Account` aggregate.

## How it works

| Code | Role |
|---|---|
| `domain/rules.py` | Pure money rules: `reserve`, `settle_buy`, `settle_sell`, `release`, `protection_price`, `portfolio_view` |
| `application/portfolio.py` | Use cases A-02, A-06, A-09, Q-09; each runs in one transaction |
| `application/sweeper.py` | Policy P-15: every 10 s, reservations with no `OrderAccepted` for 120 s → A-19 on order-service; asks again every 5 min until the engine answers |
| `adapters/postgres.py` | The store: row locks (`FOR UPDATE`, accounts first, in trader-ID order), `CHECK` constraints, migrations |
| `adapters/order_service_http.py` | A-19 `POST /internal/v1/order-expiries` |
| `adapters/partitions.py` | Which `matching.v1` partition holds a ticker (the producers' murmur2 hash) |
| `application/live.py` | Q-10: the change each transaction sends, and the hub that pushes `portfolio` / `fill` / `order_closed` |
| `adapters/postgres_listener.py` | `LISTEN portfolio_changes` on its own connection; reconnects and then refreshes every socket |

**Rules, in paise:**

| Rule | Value |
|---|---|
| BUY LIMIT reserves | `quantity × limit price` |
| BUY MARKET reserves | `quantity × protection price`; protection = last price + 10%, rounded **up** to the 5-paise tick (8820 → 9705) |
| SELL reserves | `quantity` shares; a MARKET sell's protection = last price − 10%, rounded **down** |
| Last price | From `TradeExecuted`; before any trade, the reference price ([overview §3](../../docs/00-overview.md#3-the-instruments-stocks)) |
| A trade | Buyer: reserved cash → shares at the trade price, average cost rounded half-up; seller: reserved shares → cash |
| An order finishes | Remaining 0 in a `TradeExecuted`: leftover cash (price improvement) is released, status `CONSUMED` |
| `OrderCancelled` (any reason) or `OrderExpired` | What is left is released, status `RELEASED` |
| Seed accounts | `bot-01`…`bot-20`: ₹5,00,000 + 1,000 shares of each ticker, trader_id `b0b0b0b0-00NN-4000-8000-0000000000NN`; `load-01`…`load-10`: ₹1,00,000, `10ad10ad-00NN-…`. Created at start-up if missing. |

**Idempotency.** Each `matching.v1` event's `event_id` goes into `processed_events` in the same transaction as its
change; a duplicate inserts nothing and changes nothing. A reservation repeat for the same `order_id` returns the
existing one (`200`).

**Live portfolio (Q-10) across replicas.** A phone's socket is on one replica; the trade that changes its portfolio
may be settled by another. Every use case that changes a portfolio (a reservation, a settlement, a release) sends a
Postgres `NOTIFY` on `portfolio_changes` **in its own transaction**, so it is delivered only if it commits. Every replica
`LISTEN`s, and the one holding the trader's socket sends the `fill` / `order_closed` it carries, then re-reads the
portfolio and sends `portfolio`. A trade also moves a price: sockets whose portfolio holds that ticker are re-valued at
most once a second. Identity comes only from `X-Trader-Id` (set by the gateway from the token), so a trader only ever
gets their own stream. Details: [read side §3](../../docs/03-architecture/08-read-side.md#3-portfolio-service-from-trades-to-portfolios).

**Fail-closed sweeper.** It never releases money. It asks order-service for an expiry; the money is released when the
engine's `OrderCancelled` (`ORDER_EXPIRED`) or `OrderExpired` arrives. It only sweeps tickers whose `matching.v1`
partition this replica owns and has fully read, so an unread `OrderAccepted` never looks like an orphan. If
order-service does not answer, it tries again next cycle.

## Owned data

Postgres database `portfolio` (schema: [`src/portfolio_service/migrations/`](src/portfolio_service/migrations/), applied
at start-up, recorded in `schema_migrations`): `accounts`, `holdings`, `reservations` (with `confirmed` and
`expiry_requested_at`), `settlements`, `last_prices`, `processed_events`, and `chaos_faults` (chaos's "database
degraded" switch, below).

## Events

| | |
|---|---|
| **Produces** | — on the bus. Local: `AccountOpened`, `ReservationCreated`, `TradeSettled`, `ReservationReleased`, `ExpiryRequested`, `FaultInjected`, `FaultCleared`. |
| **Consumes** | `OrderAccepted`, `TradeExecuted`, `OrderCancelled`, `OrderExpired` ← `matching.v1` (group `portfolio-service.matching.v1`). |

Schemas: [event catalog](../../docs/03-architecture/03-event-catalog.md). `tests/contract/test_events.py` applies the
catalog's own examples. A `SettlementError` (a trade the reservations do not cover; never expected) is retried 5 times,
then the event goes to `matching.v1.dlq`.

## API

Q-09 `GET /v1/portfolio` (`X-Trader-Id` from the gateway), WebSocket Q-10 `/v1/ws/portfolio` (messages in
[api/README.md](../../api/README.md#q-10--v1wsportfolio-portfolio-service-roles-trader-bot); closed with `4401` without
`X-Trader-Id`, `4404` for an unknown account), internal A-02 `POST /internal/v1/accounts`, A-06
`POST /internal/v1/reservations`, Q-11 `GET /v1/leaderboard?limit=&include_bots=`, and the chaos hook A-21
`POST /internal/v1/faults` / A-22 `DELETE /internal/v1/faults` (below). Spec:
[`api/portfolio-service.yaml`](../../api/portfolio-service.yaml). Settlement (A-09) is event-driven.

**The leaderboard (Q-11)** ranks by net worth marked to market exactly as Q-09 values a portfolio (`domain/rules.py`
`net_worth`, shared by both): cash plus every share at the last traded price, reserved ones included. Humans only by
default, bots with `include_bots=true`, load-test accounts never. Ties share a rank and the next one skips (1, 1, 3),
listed by nickname A to Z. `application/leaderboard.py` recomputes at most every 2 seconds per replica and audience;
callers that arrive together wait for one computation. The presenter dashboard polls it every 2 seconds.

Every service also exposes `/healthz`, `/readyz`, `/metrics` ([coding standards §7](../../docs/05-coding-standards.md#7-standard-operational-endpoints)).

**Business metrics:** `reservations_total{side,result}` (`created`, `repeat`, `refused`), `trades_settled_total`,
`orphan_expiry_requests_total`, `portfolio_ws_connections` (open Q-10 sockets on this replica),
`postgres_transaction_seconds{phase="pool_wait|transaction"}` (waiting for one of the pool's connections, then the
transaction itself, row-lock waits included: tells a queue for connections from slow SQL).

## Synchronous dependencies

order-service `POST /internal/v1/order-expiries` (A-19), from the background sweeper only (2 s timeout; retried next
cycle). Never on a request path.

## Scaling and operations

Default 2 replicas, agent may scale 1–5. Concurrent settlements on the same account are serialised by row locks in Postgres.

**Chaos target for "Portfolio database degraded"** (`PORTFOLIO_DB_DEGRADED`, [chaos scenarios §3.4](../../docs/03-architecture/06-chaos-scenarios.md)):
A-21 `{"fault": "DB_DEGRADED", "delay_ms": 100..5000}` stores a row in `chaos_faults`; while it is there every
reservation makes Postgres wait `delay_ms` inside its transaction (`SELECT pg_sleep`), as a struggling database would.
order-service's 500 ms reservation timeout then opens its circuit breaker (`OrderServiceCircuitOpen`), and orders get
`503 DEPENDENCY_UNAVAILABLE` at once. The switch lives in the **database**, not in a pod: every replica reads it at
start and every 2 seconds, so restarting, rolling back or scaling portfolio-service does not clear it. That is the
point of the scenario: the cause is the data store, which the SRE agent may never touch, so its correct answer is
`do_nothing` and escalate. Readiness (`SELECT 1`) is not slowed, so the pods stay Ready. A-22 (or a chaos reset)
removes the row. Only with `CHAOS_HOOKS_ENABLED=true` (staging and the demo prod; otherwise 404); only chaos-service
may call it (network policy). `application/faults.py`, `api/faults.py`, migration `003_chaos_faults.sql`.

## Run and test it alone

```bash
cd services/portfolio-service
cp .env.example .env                 # local settings (git-ignored)
uv sync                              # create the virtual environment from uv.lock
uv run pytest                        # unit, property and contract tests
uv run pytest -m integration         # the Postgres store on a real Postgres 16 (Docker, testcontainers)
HYPOTHESIS_PROFILE=deep uv run pytest tests/property
uv run ruff check . && uv run ruff format --check . && uv run mypy

make infra                           # from the repo root: Redpanda, Cassandra, Postgres and their init jobs
uv run uvicorn portfolio_service.main:create_app --factory --reload --port 8004
curl localhost:8004/readyz         # or `make up` from the repo root to run its container
```

**Ready means:**

- Redpanda, and the topic(s) `matching.v1` must exist (the init job creates them)
- Postgres: logs in as `portfolio_service` and uses database `portfolio`
- `started`: the database is migrated and seeded and Q-10's change listener runs (until then a route answers `503`
  and a WebSocket would get no live updates)
- **Not** its `matching.v1` consumer (since 2026-11-25): no answer depends on this pod's consumer, since accounts,
  reservations, holdings, last prices and the leaderboard are all in Postgres, shared by every replica, so a replica
  added under load takes requests at once while its consumer joins and catches up. The consumer's lag shows in
  `consumer_lag_messages` and `ConsumerLagHigh`; the orphan sweeper still works only on partitions this replica owns
  and has read to the end ([coding standards §7](../../docs/05-coding-standards.md#7-standard-operational-endpoints)).

## Viva Notes

**Q: Why Postgres?**
A: Settlement changes several rows that must succeed or fail together, and CHECK constraints guarantee cash never goes negative.

**Q: How is a duplicate TradeExecuted handled?**
A: Its event_id is inserted into processed_events in the same transaction; a duplicate inserts nothing, so the
transaction changes nothing. A property test redelivers random events and checks every balance stays the same.

**Q: What is an orphaned reservation, and when is its money released?**
A: Money reserved for an order the engine never confirmed (e.g. order-service crashed). After 120 s the sweeper asks
for an expiry through order-service; the money is released only when the engine answers, so it can never be traded
after it was handed back (ADR-0017).

**Q: How does the right phone get a trade that another replica settled?**
A: The settling transaction sends a Postgres NOTIFY naming the trader; it is delivered on commit to every replica, and
the one holding that trader's socket re-reads the portfolio and pushes it.

**Q: How do two simultaneous orders not spend the same rupees?**
A: Each reservation locks the account row (`SELECT … FOR UPDATE`) first, so they run one after the other; the second
sees the first's reserved cash. An integration test races 10 reservations on a real Postgres and exactly 8 fit.
