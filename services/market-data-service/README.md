# market-data-service

> **Status:** Phase 0 — design only, no code yet. This folder will follow the layout in
> [coding standards §3](../../docs/05-coding-standards.md#3-folder-layout-inside-every-service)
> (Python package `market_data_service`).

| | |
|---|---|
| **Owning team** | Market Insights Team |
| **Owner** | **MD Asif Ali** |
| **Team type** | Stream-aligned ([team topology](../../docs/04-team-topology.md)) |
| **Bounded context** | Market Data (supporting) |

## Responsibility

CQRS read side for the market: builds candles (5s, 1m, 5m), order-book depth, recent trades and last prices from matching events; streams them live over WebSockets; publishes `CandleClosed`; serves instruments; provides time travel (book at any past moment) and replay streams by reading snapshots and events from matching-engine's internal API. Hosts the chaos hook for the memory-leak scenario.

## Owned data

Cassandra keyspace `market_data`: `candles`, `trades_by_ticker`, `last_prices`. Depth is kept in memory (rebuilt on start-up).

## Events

| | |
|---|---|
| **Produces** | `CandleClosed` → `candles.v1`. Local: `FaultInjected`, `FaultCleared`. |
| **Consumes** | `OrderAccepted`, `TradeExecuted`, `OrderCancelled` ← `matching.v1`. |

Schemas: [event catalog](../../docs/03-architecture/03-event-catalog.md).

## API

Q-02 `GET /v1/instruments`, Q-03 `GET /v1/market/{ticker}/candles`, Q-04 `GET /v1/market/{ticker}/order-book`, Q-05 `GET /v1/market/{ticker}/trades`, Q-12 `GET /v1/time-travel/{ticker}`; WebSockets Q-06 `/v1/ws/market/{ticker}`, Q-13 `/v1/ws/replay/{ticker}`; internal A-12 `POST /internal/v1/faults`, A-13 `DELETE /internal/v1/faults`. Spec: [`api/market-data-service.yaml`](../../api/market-data-service.yaml).

Every service also exposes `/healthz`, `/readyz`, `/metrics` ([coding standards §7](../../docs/05-coding-standards.md#7-standard-operational-endpoints)).

## Synchronous dependencies

`matching-engine` `GET /internal/v1/snapshots/{ticker}` (Q-15) and `GET /internal/v1/events` (Q-14) for time travel and replay.

## Scaling and operations

Default 1 replica, agent may scale 1–5 (each replica consumes the full stream for its WebSocket clients; deterministic `CandleClosed` IDs make duplicates harmless). Memory limit 512 MiB. Chaos target for `MEMORY_LEAK`.

## Viva Notes

**Q: How does time travel work?**
A: Nearest snapshot before T + apply the events up to T in a fresh in-memory book; plus candles and trades up to T. Live state is never touched.

**Q: Why can't it read the event store directly?**
A: Data ownership: the event store belongs to matching-engine, which exposes an internal API for it.

**Q: What happens to trading when this service is down?**
A: Nothing — trading and portfolios don't depend on it. Charts freeze and catch up after restart.
