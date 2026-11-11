# market-data-service

> **Status:** Phase 4 — candles (5s, 1m, 5m), order-book depth, recent trades, last prices,
> `CandleClosed`, the live market WebSocket (Q-06), time travel (Q-12) and replay (Q-13) are built. The chaos hooks
> (A-12, A-13) are Phase 7, and answer `501` until then. Layout:
> [coding standards §3](../../docs/05-coding-standards.md#3-folder-layout-inside-every-service) (package
> `market_data_service`).

| | |
|---|---|
| **Owning team** | Market Insights Team |
| **Owner** | **MD Asif Ali** |
| **Team type** | Stream-aligned ([team topology](../../docs/04-team-topology.md)) |
| **Bounded context** | Market Data (supporting) |

## Responsibility

CQRS read side for the market: builds candles (5s, 1m, 5m), order-book depth, recent trades and last prices from matching events; streams them live over WebSockets; publishes `CandleClosed`; serves instruments; provides time travel (book at any past moment) and replay streams by reading snapshots and events from matching-engine's internal API. Hosts the chaos hook for the memory-leak scenario.

## How it works

How events become candles and depth, and why all of it can be rebuilt at any time:
[read side](../../docs/03-architecture/08-read-side.md).

| Code | Role |
|---|---|
| `domain/candles.py` | `CandleSeries` (live: trades by `executed_at`, the clock, late corrections) and `rebuild` (from trades alone) |
| `domain/depth.py` | Depth per price level from `OrderAccepted` / `TradeExecuted` / `OrderCancelled`; never crossed |
| `domain/market.py` | One ticker's read model at one `sequence`: depth, candles, last price, today's figures, recent trades |
| `application/projection.py` | Sequence order, gap filling from Q-14, the start-up catch-up, the candle clock, saving, `CandleClosed` |
| `application/hub.py` | The Q-06 WebSocket fan-out: snapshot first, then `trade`, `book` (≤ 4/s) and `candle` |
| `adapters/matching_feed.py` | `matching.v1`: every partition, **no consumer group**, from the end |
| `adapters/engine_http.py` | matching-engine Q-14 (events, paged) and Q-15 (snapshot) |
| `adapters/cassandra_store.py` | Keyspace `market_data`; one logged batch per event |

**Rules:**

| Rule | Value |
|---|---|
| Order | Per ticker, strictly by `sequence`: a repeat is skipped; a gap is filled from the event store (Q-14) first |
| Start-up | Depth from the engine's latest snapshot (Q-15), the rest from what was saved; the events after both from Q-14. The feed is at the end of every partition *before* the catch-up, so nothing falls in between |
| Candle | Bucket `[start, start + interval)` of `executed_at`; open/close = first/last trade by sequence; a bucket without trades is flat at the previous close (the reference price before the first trade) |
| Closing | When event time passes the bucket, or the clock is 0.5 s past its end while the feed is caught up. A later trade for a closed bucket corrects it (and the flat ones after it) |
| `CandleClosed` | One per closed candle and interval, ID = UUIDv5 of `ticker|interval|bucket_start`; best effort (dropped if Redpanda is down) |
| Depth | Leaves out the incoming order while it still crosses the book mid-match, so it is never crossed |
| Q-02 `change` | Last price − reference price; today = the UTC day |
| Saving | Every event's changes (its candles, its trade, the new `sequence`) in one logged batch, before the next event |

## Owned data

Cassandra keyspace `market_data` ([`src/market_data_service/migrations/`](src/market_data_service/migrations/), applied at
start-up): `candles`, `trades_by_ticker`, `last_prices` (also the last applied `sequence` and today's figures). Depth is
kept in memory and rebuilt at start-up. Every table can be dropped: a restart rebuilds it from the event store.

## Events

| | |
|---|---|
| **Produces** | `CandleClosed` → `candles.v1`. Local: `FaultInjected`, `FaultCleared` (Phase 7). |
| **Consumes** | `OrderAccepted`, `TradeExecuted`, `OrderCancelled` ← `matching.v1` (no consumer group: each replica reads every partition). `OrderCancelRejected` and `OrderExpired` only advance the sequence. |

Schemas: [event catalog](../../docs/03-architecture/03-event-catalog.md). `tests/contract/test_events.py` checks every
`CandleClosed` against `contracts/events/` and applies the catalog's own examples.

## API

Q-02 `GET /v1/instruments`, Q-03 `GET /v1/market/{ticker}/candles`, Q-04 `GET /v1/market/{ticker}/order-book`, Q-05
`GET /v1/market/{ticker}/trades`; WebSocket Q-06 `/v1/ws/market/{ticker}` (messages in
[api/README.md](../../api/README.md#q-06--v1wsmarketticker-market-data-service-any-role)); Q-12
`GET /v1/time-travel/{ticker}?at=` and the replay WebSocket Q-13 `/v1/ws/replay/{ticker}` (both below). Phase 7: internal A-12 `POST /internal/v1/faults`, A-13
`DELETE /internal/v1/faults`. Spec: [`api/market-data-service.yaml`](../../api/market-data-service.yaml);
`tests/contract/test_response_schemas.py` validates the answers and the WebSocket messages against it. Until the
start-up catch-up is done the queries answer `503 DEPENDENCY_UNAVAILABLE` and the WebSocket closes with `1011`.

**Time travel (Q-12)** rebuilds the whole book as it was at `at`: matching-engine's nearest snapshot at or before `at`
(Q-15, with its finished and expired orders) plus the events after it up to `at` (Q-14), applied to a fresh
`ReplayBook` (`domain/replay_book.py`, apply only, this service's own code). The answer carries the book's
`state_hash`, computed by the engine's rule (a contract test checks the hash the engine's spec states for its example).
**The proof:** when `at` is at or after the ticker's latest event, the engine's live book (Q-16) is read first, the
rebuild stops at exactly its sequence, and `verification` says whether the hashes match. One sequence on both sides
means the check works while bots trade.
The 1-minute candles and recent trades come from this service's tables; the candle holding `at` is recomputed from the
trades up to `at`. Each replica rebuilds at most 4 books at once and keeps one of those places for the admin role, so
the presenter is never refused; beyond that, `503 OVERLOADED`. The book is a private copy: the live projection is never
touched. `scripts/time_travel_check.py` measures seeks on the running stack (over a full hour of bot trading: 100
seeks, p50 41 ms, p95 70 ms, slowest 143 ms; never more than 498 events applied after a snapshot).

**Replay (Q-13)** (`application/replay.py`) rebuilds the book at `from` like time travel and sends it as the first
frame, then walks the stored events up to `to`: one frame per command (same `causation_id`), after waiting the original
time between commands divided by the speed. The viewer sends `pause`, `resume` and `speed`; market time is a `Pacer`
that runs at speed × the wall clock and reacts at once. When market time reaches `to`, `end` carries the hash of the
book at `to` (equal to Q-12's at `to`). At most 4 replays per replica, one place kept for the admin role (`1013` when
full); a disconnect or more than 5 minutes paused ends the replay and frees its place. Checked on the running stack: 2
minutes of bot trading at 60× gave 344 frames in 2.06 s, and the `end` hash equalled Q-12's at `to`.

**Isolation, measured** (`make test-e2e`, 2026-11-12): four replays at once while the 20 bots trade (three traders and
the presenter; a fourth trader is refused with `1013`, the presenter is not). Placing an order: p95 **24 ms** without
replays, **20 ms** during the four, so the replays cost the live market nothing visible. Every live book still verified
afterwards, and each replay ended on the hash Q-12 rebuilds at its `to`. The e2e bound is 500 ms, for slower CI
runners.

Every service also exposes `/healthz`, `/readyz`, `/metrics` ([coding standards §7](../../docs/05-coding-standards.md#7-standard-operational-endpoints)).

**Business metrics:** `market_ws_connections` (open Q-06 sockets on this replica), `candles_closed_total{interval}`,
`projection_gaps_filled_total`.

## Synchronous dependencies

`matching-engine` `GET /internal/v1/snapshots/{ticker}` (Q-15) and `GET /internal/v1/events` (Q-14): the start-up
catch-up, gap filling, time travel and replay; `GET /internal/v1/books/{ticker}` (Q-16) when a time-travel
request reaches "now" (the proof below). If the engine is down at start-up, the service retries and is
not ready; trading is not affected.

## Scaling and operations

Default 1 replica, agent may scale 1–5 (each replica consumes the full stream for its WebSocket clients; deterministic `CandleClosed` IDs make duplicates harmless). Memory limit 512 MiB. Chaos target for `MEMORY_LEAK`.

## Run and test it alone

```bash
cd services/market-data-service
cp .env.example .env                 # local settings (git-ignored)
uv sync                              # create the virtual environment from uv.lock
uv run pytest                        # unit, property, WebSocket and contract tests (contract tests read ../../api/)
uv run pytest -m integration --no-cov  # the store on a real Cassandra 5 (Docker, testcontainers)
HYPOTHESIS_PROFILE=deep uv run pytest tests/property
uv run ruff check . && uv run ruff format --check . && uv run mypy

make infra                           # from the repo root: Redpanda, Cassandra, Postgres and their init jobs
uv run uvicorn market_data_service.main:create_app --factory --reload --port 8003
curl localhost:8003/readyz         # or `make up` from the repo root to run its container
```

**Ready means:**

- Redpanda, and the topic(s) `matching.v1`, `candles.v1` must exist (the init job creates them)
- Cassandra: logs in with its own role `market_data_role` and uses keyspace `market_data` only
- `projection`: the market is rebuilt from the event store and `matching.v1` has been read to the end once

## Viva Notes

**Q: How does time travel work?**
A: Nearest snapshot before T + apply the events up to T in a fresh in-memory book; plus candles and trades up to T. Live state is never touched.

**Q: Why can't it read the event store directly?**
A: Data ownership: the event store belongs to matching-engine, which exposes an internal API for it.

**Q: What happens to trading when this service is down?**
A: Nothing — trading and portfolios don't depend on it. Charts freeze and catch up after restart.

**Q: Why no consumer group?**
A: Every replica serves its own WebSocket clients, so each needs every event. The stored `sequence` per ticker says what
has been applied, and anything missed is read from the event store, so there are no offsets to commit.

**Q: How do you know the candles are right?**
A: A property test drives random trades and clock ticks (including trades that arrive after the clock closed their
bucket) and checks that the live and stored candles equal candles rebuilt from the trades alone.
