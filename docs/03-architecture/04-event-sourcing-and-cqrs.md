# 04 — Event Sourcing and CQRS

## 1. The two ideas in plain words

> **Event sourcing:** instead of storing *current state* ("CHAIWALA book has 30 @ ₹246.00") and overwriting it,
> store *every change* as an immutable event ("order accepted", "trade 20 @ ₹245.50", …). Current state is whatever
> you get by replaying the events in order. Like a bank statement: the balance is derived from the transactions.
>
> **CQRS (Command Query Responsibility Segregation):** use one model to *change* data (the write side) and separate
> models, shaped for each screen, to *read* data (the read side). The read models are built from the write side's
> events.

They are independent ideas that fit together well: the event stream from the write side is exactly what you need to
build read models.

## 2. Where we use them (and where we don't)

| Part | Event sourced? | Why |
|---|---|---|
| `matching-engine` (market) | ✅ **Yes — fully** | Time travel and replay need every change; a fair exchange needs a complete audit trail; the book is rebuilt from events after a crash ([ADR-0005](../adr/0005-event-sourcing-for-matching-engine.md)). |
| `order-service` | Partly — stores order events (`order_events`) plus current-state tables | Order history is useful, but queries are simple; the current-state tables answer queries directly. |
| `portfolio-service` | ❌ No — a CQRS **read model** in Postgres, built from `matching.v1` | Balances need transactions and constraints; they can always be re-derived from the market event store ([ADR-0006](../adr/0006-cqrs-portfolio-read-model.md)). |
| `market-data-service` | ❌ No — CQRS **read models** (candles, depth, trades) | Pure projections of market events. |
| `sre-agent` | Append-only timeline per incident | Natural audit log of agent decisions. |

**Why not event-source everything?** Event sourcing adds complexity (snapshots, versioning, replay tooling). We use it
where it pays for itself — the market — and use simpler models elsewhere.

## 3. The write side and the read side

```mermaid
flowchart LR
    classDef write fill:#ffd8a8,stroke:#d9480f,color:#000
    classDef read fill:#b2f2bb,stroke:#2b8a3e,color:#000
    classDef bus fill:#d0bfff,stroke:#5f3dc4,color:#000

    subgraph WRITE["WRITE SIDE (commands)"]
        C1["POST /v1/orders<br/>DELETE /v1/orders/id"]:::write --> OS["order-service<br/>validate + reserve"]:::write
        OS -- "orders.v1" --> ME["matching-engine<br/>OrderBook aggregates"]:::write
        ME --> ES[("Event store<br/>Cassandra matching_engine<br/>market_events + book_snapshots")]:::write
    end
    ES -. "publish after append" .-> BUS(["matching.v1"]):::bus
    subgraph READ["READ SIDE (queries)"]
        BUS --> MD["market-data-service<br/>candles, depth, trades,<br/>last price"]:::read
        BUS --> PS["portfolio-service<br/>portfolios, leaderboard"]:::read
        BUS --> OSR["order-service<br/>my orders, status"]:::read
        MD --> Q1["GET /v1/market/...<br/>WS /v1/ws/market/..."]:::read
        PS --> Q2["GET /v1/portfolio<br/>GET /v1/leaderboard"]:::read
        OSR --> Q3["GET /v1/orders"]:::read
        MD -- "time travel:<br/>GET /internal/v1/events, snapshots" --> ME
    end
```

`order-service` appears on both sides: it accepts commands (write) and also keeps an order-status projection from
`matching.v1` (read). That's fine — CQRS separates *models*, not necessarily *services*.

## 4. How the event store works

### Appending (inside matching-engine)

For each incoming command (`OrderPlaced`, `OrderCancelRequested` or `OrderExpiryRequested`) from `orders.v1`:

1. **Deduplicate:** if `command_id` (the incoming `event_id`) is in `processed_commands`, skip to step 5.
2. **Decide:** run the matching algorithm on the in-memory `OrderBook` for that ticker. This *produces* events
   (`OrderAccepted`, zero or more `TradeExecuted`, maybe `OrderCancelled`; or `OrderExpired`) — it does not mutate yet.
3. **Append:** write those events with consecutive `sequence` numbers to `market_events`, plus the `command_id` into
   `processed_commands`, as one Cassandra **logged batch** (all-or-nothing).
4. **Apply:** apply the events to the in-memory book (the *same* `apply(event)` function used during replay).
5. **Publish:** send any events after the `publish_cursor` to `matching.v1`, then advance the cursor.
6. **Commit** the Kafka offset of the command.

If the process crashes anywhere, restart = load latest snapshot + replay later events to rebuild books → publish
anything after the cursor → resume consuming from the last committed offset. Duplicates are possible, losses are not.

> **Decide vs apply.** "Decide" contains the business rules (matching) and can change between versions. "Apply"
> is dumb: "an `OrderAccepted` adds the order to the book; a `TradeExecuted` reduces both orders; remaining 0 removes
> it; an `OrderCancelled` removes it; an `OrderExpired` marks the ID as dead". Replay only ever calls *apply*. This is
> why history never changes even if we fix a bug in matching.

### Snapshots

Every **500 events per ticker**, the engine saves the whole book for that ticker into `book_snapshots` with its
`sequence` and time (local event `BookSnapshotTaken`). To rebuild a book at any point we load the nearest earlier
snapshot and apply at most ~500 events, instead of the whole day.

### Single writer per ticker

Price-time priority needs **one** decision-maker per ticker: two engines on the same book could both match the same
resting order. We guarantee this by running **one matching-engine replica** (its max replicas is 1) and keying
`orders.v1` by ticker. If we ever needed more throughput, the safe way is to give each replica *different
partitions* (and therefore different tickers) — never two replicas on one ticker.

### The outbox in order-service

order-service has the same "save + publish" problem: it appends `OrderPlaced` to `order_events` with
`published = false`, publishes to `orders.v1`, then sets `published = true`. A background **outbox relay** re-publishes
any rows still `false` after 5 seconds. Result: once the API returns `202`, the order *will* reach the engine,
possibly twice — and the engine deduplicates via `processed_commands`. One limit: the relay never publishes an
`OrderPlaced` older than **60 s**; it marks the order `REJECTED` (`PUBLISH_TIMEOUT`) instead, so the trader learns quickly
that the order did not get through ([ADR-0011](../adr/0011-synchronous-reservation-over-saga.md)). The same outbox carries
`OrderCancelRequested` and `OrderExpiryRequested`; a cancel or expiry for an order is never published before that order's
`OrderPlaced`.

**Orphaned reservations are released fail-closed** ([ADR-0017](../adr/0017-fail-closed-orphan-release.md)): after 120 s
without `OrderAccepted`, portfolio-service asks order-service to publish an `OrderExpiryRequested`. It lands in the same
partition as the order, so the engine answers only after any `OrderPlaced` already sent: it cancels a resting order
(`ORDER_EXPIRED`) or records an `OrderExpired` tombstone for one it never saw. Money is released only on that answer.

> **Outbox pattern:** write the outgoing message into your own database in the same step as your state change, and
> publish it from there. It avoids the "saved but not published" (or the reverse) problem without distributed
> transactions.

## 5. Time travel and replay

Sequence diagram: [01-seeds/03-sequence-diagrams.md §5](../01-seeds/03-sequence-diagrams.md#5-time-travel-and-replay-j-20--q-12-q-13-q-14-q-15).

**Snapshot at time T** (`GET /v1/time-travel/{ticker}?at=T`, Q-12):

```text
snapshot   = matching-engine GET /internal/v1/snapshots/{ticker}?at=T     # nearest snapshot ≤ T
events     = matching-engine GET /internal/v1/events?ticker=…&after_sequence=snapshot.sequence&until=T  (paged)
book       = OrderBook.from_snapshot(snapshot)
for e in events: book.apply(e)                                              # apply only, never decide
candles    = market_data.candles where bucket_start ≤ T
trades     = market_data.trades_by_ticker where executed_at ≤ T (latest 50)
return { as_of: T, as_of_sequence: last(events).sequence, book: book.depth(), candles, trades }
```

**Replay stream** (`WS /v1/ws/replay/{ticker}?from=…&to=…&speed=…`, Q-13): rebuild the book at `from` as above, then
walk the events up to `to`, sleeping `(next.occurred_at − current.occurred_at) / speed` between them, and push a
`frame` after each group of events with the same `causation_id` (one command's effects), so viewers never see a
half-applied match. Allowed speeds: 1, 2, 5, 10, 60.

**Time travel never touches live state.** It builds a private copy of the book in memory for that request. Ten people
can time-travel to ten different moments while the live market keeps trading.

**Rebuilding a read model from scratch** (e.g. after a bug in candle calculation): drop the projection tables and
re-read the full history from matching-engine's events API — not from Redpanda, whose retention is only 7 days.

## 6. Eventual consistency — what the user actually sees

Read models are updated *after* the write side, usually within tens of milliseconds. Consequences we design for:

| Situation | What happens | How the UI handles it |
|---|---|---|
| Order just placed | `POST` returns `202` with status `PENDING` | Shows "pending" spinner until the WebSocket brings `OPEN`/fill. |
| Trade just happened | Chart may update a few ms before the portfolio | Acceptable; both converge quickly. |
| Leaderboard | Recomputed every 2 s from Postgres | Labelled "updated every few seconds". |
| market-data-service is down (memory-leak scenario) | Charts freeze; trading and portfolios still work | UI shows "market data reconnecting"; after restart the service catches up from Redpanda offsets. |

## 7. Idempotency summary

| Consumer | Dedup key | Stored where |
|---|---|---|
| matching-engine | `command_id` (= incoming `event_id`) | `processed_commands` (same logged batch as the events) |
| order-service | `event_id` | `processed_events` |
| portfolio-service | `event_id` | `processed_events` (same SQL transaction as settlement) |
| market-data-service | `sequence` per ticker (ignore ≤ last applied) | in memory + `last_prices` |
| bot-traders | `event_id` (deterministic for `CandleClosed`) | in memory |
| chaos-service | `event_id` | in memory |

## Viva Notes

**Q: What is event sourcing, in one sentence?**
A: Storing every change as an immutable event and deriving current state by replaying those events, rather than
overwriting the current state.

**Q: What is CQRS and how do you use it?**
A: Separating the model that handles commands from the models that answer queries. Our write side is order-service plus
matching-engine with its event store; our read side is market-data-service and portfolio-service, built from
`matching.v1` events.

**Q: How does time travel work?**
A: Load the nearest snapshot before time T from matching-engine, fetch the events between that snapshot and T, apply
them to a fresh in-memory book, and return it along with candles and trades up to T. Live state is never touched.

**Q: Why snapshots?**
A: Without them, rebuilding a book means replaying every event since the start. A snapshot every 500 events limits any
rebuild to at most about 500 events.

**Q: Why is there only one matching-engine replica — isn't that a single point of failure?**
A: It's a deliberate trade-off: correctness of price-time priority needs a single writer per ticker. Availability comes
from fast recovery: Kubernetes restarts it, and it rebuilds from snapshot + events in seconds, while orders wait safely
in Redpanda.
