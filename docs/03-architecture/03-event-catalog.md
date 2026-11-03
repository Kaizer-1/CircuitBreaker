# 03 — Event Catalog

> **This document is the contract for every event in the system.** Event names, topic names and field names here
> are the single source of truth; the docs, sequence diagrams and code must match them exactly.
>
> JSON Schemas use **JSON Schema draft 2020-12**. In Phase 1 they will be extracted into
> `contracts/events/*.schema.json` and checked in CI.

## Integration events vs local events

| | Integration event | Local event |
|---|---|---|
| Published on Redpanda? | **Yes** | No |
| Who depends on it | Other services | Only the owning service |
| Can change freely? | No — it is a public contract, versioned | Yes — internal detail |
| Examples | `OrderPlaced`, `TradeExecuted` | `ReservationCreated`, `BookSnapshotTaken` |

We publish an event only when another service needs it. Fewer public contracts = less coordination.

## Naming conventions

| Thing | Convention | Example |
|---|---|---|
| Event type | `PascalCase`, past tense, noun + verb | `OrderCancelRequested` |
| Topic | `<stream>.v<major>` (lower-case, dot-separated) | `matching.v1` |
| Fields | `snake_case`; money ends in `_paise`; times end in `_at` (ISO-8601 UTC) | `limit_price_paise`, `executed_at` |
| IDs | UUID strings, field ends in `_id` (except `incident_id`: `INC-0001` style for readability) | `order_id` |
| Enum values | `UPPER_SNAKE_CASE` | `PARTIALLY_FILLED` |
| Consumer group | `<service>.<topic>` | `portfolio-service.matching.v1` |
| Dead-letter topic | `<topic>.dlq` | `matching.v1.dlq` |

## Topics

| Topic | Producer | Consumers | Message key | Partitions | Retention | Events |
|---|---|---|---|---|---|---|
| `orders.v1` | `order-service` | `matching-engine` | `ticker` | 5 | 7 days | `OrderPlaced`, `OrderCancelRequested` |
| `matching.v1` | `matching-engine` | `order-service`, `portfolio-service`, `market-data-service` | `ticker` | 5 | 7 days | `OrderAccepted`, `TradeExecuted`, `OrderCancelled`, `OrderCancelRejected` |
| `candles.v1` | `market-data-service` | `bot-traders` | `ticker` | 5 | 1 day | `CandleClosed` |
| `incidents.v1` | `sre-agent` | `chaos-service` | `incident_id` | 1 | 30 days | `IncidentOpened`, `RemediationProposed`, `RemediationApproved`, `RemediationDeclined`, `RemediationExecuted`, `IncidentResolved` |

**Why key by ticker?** Redpanda guarantees order only *within a partition*. Keying by ticker puts all of one stock's
orders and cancels in one partition, so the matching engine sees them in the exact order they were placed. Different
tickers can be processed in parallel.

**Why is Redpanda retention only 7 days?** Redpanda is the *transport*. The permanent record is the Cassandra event
store, which is replayable through matching-engine's API forever.

**One topic per producer stream** (rather than one topic per event type) keeps related events in order: an
`OrderAccepted` is always seen before the `TradeExecuted` it caused.

## Delivery guarantees (apply to all consumers)

- **At-least-once delivery.** A consumer commits its offset only *after* processing. A crash may cause redelivery.
- **Idempotent consumers.** Each consumer records processed `event_id`s (in the same transaction as its change,
  where the store allows) and skips duplicates.
- **Unknown fields are ignored** (tolerant reader), so producers can add optional fields without breaking anyone.
- **Poison messages** (fail 5 times) go to `<topic>.dlq` with the error attached, and an alert fires. Dead-letter topics
  have **1 partition and 7-day retention** (a Phase 1 choice; they only need to hold failures for inspection).

## Versioning rules

| Change | Allowed in same version? | How |
|---|---|---|
| Add an optional field | ✅ Yes | Consumers ignore unknown fields. |
| Add a new event type to a topic | ✅ Yes | Consumers ignore unknown `event_type`s. |
| Remove/rename a field, change a type or meaning | ❌ No | Bump `event_version`, publish to a new topic `<stream>.v2`, run v1 and v2 in parallel until all consumers have moved, then retire v1. |

---

## The envelope (every integration event)

Every message on every topic has the same outer structure. Only `data` differs per event type.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://circuitbreaker.local/contracts/events/envelope.schema.json",
  "title": "EventEnvelope",
  "type": "object",
  "required": ["event_id", "event_type", "event_version", "occurred_at", "producer", "correlation_id", "data"],
  "properties": {
    "event_id":       { "type": "string", "format": "uuid", "description": "Unique ID; consumers deduplicate on it" },
    "event_type":     { "type": "string", "description": "PascalCase past-tense name, e.g. TradeExecuted" },
    "event_version":  { "type": "integer", "minimum": 1 },
    "occurred_at":    { "type": "string", "format": "date-time", "description": "UTC, millisecond precision" },
    "producer":       { "type": "string", "enum": ["order-service", "matching-engine", "market-data-service", "sre-agent"] },
    "environment":    { "type": "string", "enum": ["dev", "staging", "prod"] },
    "correlation_id": { "type": "string", "description": "From the X-Correlation-Id of the originating request" },
    "causation_id":   { "type": ["string", "null"], "format": "uuid", "description": "event_id of the event that caused this one" },
    "data":           { "type": "object" }
  },
  "additionalProperties": false
}
```

Shared definitions used below:

```json
{
  "$defs": {
    "ticker":      { "type": "string", "enum": ["CHAIWALA", "JUGAAD", "DABBA", "MONSOON", "FILMY"] },
    "side":        { "type": "string", "enum": ["BUY", "SELL"] },
    "order_type":  { "type": "string", "enum": ["LIMIT", "MARKET"] },
    "price_paise": { "type": "integer", "minimum": 5, "multipleOf": 5, "description": "Price in paise; multiple of the 5-paise tick" },
    "quantity":    { "type": "integer", "minimum": 1, "maximum": 10000 },
    "uuid":        { "type": "string", "format": "uuid" },
    "timestamp":   { "type": "string", "format": "date-time" },
    "sequence":    { "type": "integer", "minimum": 1, "description": "Per-ticker, strictly increasing, assigned by matching-engine" },
    "service":     { "type": "string", "enum": ["api-gateway", "order-service", "matching-engine", "market-data-service", "portfolio-service", "bot-traders", "sre-agent", "chaos-service"] },
    "agent_action": { "type": "string", "enum": ["rollback_deployment", "scale_deployment", "restart_deployment", "do_nothing"] }
  }
}
```

---

## Topic `orders.v1` (producer: `order-service`)

### `OrderPlaced`

A trader's order passed validation and its cash or shares are reserved. It is now waiting to be matched.

- **Producer:** `order-service` **Consumers:** `matching-engine` (policy P-01: match it)
- **Key:** `ticker`

```json
{
  "title": "OrderPlaced.data",
  "type": "object",
  "required": ["order_id", "trader_id", "ticker", "side", "order_type", "quantity", "reservation_id", "placed_at"],
  "properties": {
    "order_id":               { "$ref": "#/$defs/uuid" },
    "trader_id":              { "$ref": "#/$defs/uuid" },
    "ticker":                 { "$ref": "#/$defs/ticker" },
    "side":                   { "$ref": "#/$defs/side" },
    "order_type":             { "$ref": "#/$defs/order_type" },
    "quantity":               { "$ref": "#/$defs/quantity" },
    "limit_price_paise":      { "oneOf": [{ "$ref": "#/$defs/price_paise" }, { "type": "null" }], "description": "Required for LIMIT, null for MARKET" },
    "protection_price_paise": { "oneOf": [{ "$ref": "#/$defs/price_paise" }, { "type": "null" }], "description": "Required for MARKET, null for LIMIT" },
    "reservation_id":         { "$ref": "#/$defs/uuid" },
    "placed_at":              { "$ref": "#/$defs/timestamp" }
  }
}
```

```json
{
  "event_id": "0b8f6a52-7d1e-4c55-9a43-1f2d3c4b5a61",
  "event_type": "OrderPlaced",
  "event_version": 1,
  "occurred_at": "2026-11-20T10:42:01.250Z",
  "producer": "order-service",
  "environment": "prod",
  "correlation_id": "req-5f1c2a",
  "causation_id": null,
  "data": {
    "order_id": "7f3c9e10-2b4d-4e6f-8a1b-3c5d7e9f0a12",
    "trader_id": "3d2a1b0c-9e8f-4a7b-8c6d-5e4f3a2b1c0d",
    "ticker": "CHAIWALA",
    "side": "BUY",
    "order_type": "LIMIT",
    "quantity": 50,
    "limit_price_paise": 24600,
    "protection_price_paise": null,
    "reservation_id": "c1d2e3f4-a5b6-4c7d-8e9f-0a1b2c3d4e5f",
    "placed_at": "2026-11-20T10:42:01.248Z"
  }
}
```

### `OrderCancelRequested`

The owner asked to cancel an order. Whether it *can* be cancelled is decided by the matching engine.

- **Producer:** `order-service` **Consumers:** `matching-engine` (policy P-02)
- **Key:** `ticker`

```json
{
  "title": "OrderCancelRequested.data",
  "type": "object",
  "required": ["order_id", "trader_id", "ticker", "requested_at"],
  "properties": {
    "order_id":     { "$ref": "#/$defs/uuid" },
    "trader_id":    { "$ref": "#/$defs/uuid" },
    "ticker":       { "$ref": "#/$defs/ticker" },
    "requested_at": { "$ref": "#/$defs/timestamp" }
  }
}
```

```json
{
  "event_id": "5a6b7c8d-9e0f-4a1b-8c2d-3e4f5a6b7c8d",
  "event_type": "OrderCancelRequested",
  "event_version": 1,
  "occurred_at": "2026-11-20T10:43:10.004Z",
  "producer": "order-service",
  "environment": "prod",
  "correlation_id": "req-9a8b7c",
  "causation_id": null,
  "data": {
    "order_id": "7f3c9e10-2b4d-4e6f-8a1b-3c5d7e9f0a12",
    "trader_id": "3d2a1b0c-9e8f-4a7b-8c6d-5e4f3a2b1c0d",
    "ticker": "CHAIWALA",
    "requested_at": "2026-11-20T10:43:10.001Z"
  }
}
```

---

## Topic `matching.v1` (producer: `matching-engine`)

Every event on this topic has a `sequence` in `data`: per ticker, strictly increasing, no gaps. It is the order of
events in the event store and the basis of replay.

### `OrderAccepted`

The engine took the order into processing for its ticker. Any trades it causes follow immediately.

- **Producer:** `matching-engine` **Consumers:** `order-service` (status `OPEN`), `market-data-service` (depth),
  `portfolio-service` (marks the reservation as confirmed; unconfirmed reservations are released after 120 s —
  [ADR-0011](../adr/0011-synchronous-reservation-over-saga.md))
- **Key:** `ticker`

```json
{
  "title": "OrderAccepted.data",
  "type": "object",
  "required": ["sequence", "ticker", "order_id", "trader_id", "side", "order_type", "quantity", "accepted_at"],
  "properties": {
    "sequence":               { "$ref": "#/$defs/sequence" },
    "ticker":                 { "$ref": "#/$defs/ticker" },
    "order_id":               { "$ref": "#/$defs/uuid" },
    "trader_id":              { "$ref": "#/$defs/uuid" },
    "side":                   { "$ref": "#/$defs/side" },
    "order_type":             { "$ref": "#/$defs/order_type" },
    "quantity":               { "$ref": "#/$defs/quantity" },
    "limit_price_paise":      { "oneOf": [{ "$ref": "#/$defs/price_paise" }, { "type": "null" }] },
    "protection_price_paise": { "oneOf": [{ "$ref": "#/$defs/price_paise" }, { "type": "null" }] },
    "accepted_at":            { "$ref": "#/$defs/timestamp" }
  }
}
```

```json
{
  "event_id": "e1f2a3b4-c5d6-4e7f-8a9b-0c1d2e3f4a5b",
  "event_type": "OrderAccepted",
  "event_version": 1,
  "occurred_at": "2026-11-20T10:42:01.262Z",
  "producer": "matching-engine",
  "environment": "prod",
  "correlation_id": "req-5f1c2a",
  "causation_id": "0b8f6a52-7d1e-4c55-9a43-1f2d3c4b5a61",
  "data": {
    "sequence": 1041,
    "ticker": "CHAIWALA",
    "order_id": "7f3c9e10-2b4d-4e6f-8a1b-3c5d7e9f0a12",
    "trader_id": "3d2a1b0c-9e8f-4a7b-8c6d-5e4f3a2b1c0d",
    "side": "BUY",
    "order_type": "LIMIT",
    "quantity": 50,
    "limit_price_paise": 24600,
    "protection_price_paise": null,
    "accepted_at": "2026-11-20T10:42:01.262Z"
  }
}
```

### `TradeExecuted`

A buy and a sell matched. Trade price is always the **resting** order's price.

- **Producer:** `matching-engine`
- **Consumers:** `order-service` (filled quantity, status), `portfolio-service` (settlement, release leftover when
  an order's remaining quantity reaches 0, last price), `market-data-service` (last price, candles, trades, depth)
- **Key:** `ticker`

```json
{
  "title": "TradeExecuted.data",
  "type": "object",
  "required": ["sequence", "ticker", "trade_id", "price_paise", "quantity", "buy_order_id", "sell_order_id",
               "buyer_trader_id", "seller_trader_id", "aggressor_side",
               "buy_order_remaining_quantity", "sell_order_remaining_quantity", "executed_at"],
  "properties": {
    "sequence":                      { "$ref": "#/$defs/sequence" },
    "ticker":                        { "$ref": "#/$defs/ticker" },
    "trade_id":                      { "$ref": "#/$defs/uuid" },
    "price_paise":                   { "$ref": "#/$defs/price_paise" },
    "quantity":                      { "$ref": "#/$defs/quantity" },
    "buy_order_id":                  { "$ref": "#/$defs/uuid" },
    "sell_order_id":                 { "$ref": "#/$defs/uuid" },
    "buyer_trader_id":               { "$ref": "#/$defs/uuid" },
    "seller_trader_id":              { "$ref": "#/$defs/uuid" },
    "aggressor_side":                { "$ref": "#/$defs/side" },
    "buy_order_remaining_quantity":  { "type": "integer", "minimum": 0 },
    "sell_order_remaining_quantity": { "type": "integer", "minimum": 0 },
    "executed_at":                   { "$ref": "#/$defs/timestamp" }
  }
}
```

```json
{
  "event_id": "f6a7b8c9-d0e1-4f2a-8b3c-4d5e6f7a8b9c",
  "event_type": "TradeExecuted",
  "event_version": 1,
  "occurred_at": "2026-11-20T10:42:01.263Z",
  "producer": "matching-engine",
  "environment": "prod",
  "correlation_id": "req-5f1c2a",
  "causation_id": "0b8f6a52-7d1e-4c55-9a43-1f2d3c4b5a61",
  "data": {
    "sequence": 1042,
    "ticker": "CHAIWALA",
    "trade_id": "a0b1c2d3-e4f5-4a6b-8c7d-8e9f0a1b2c3d",
    "price_paise": 24550,
    "quantity": 20,
    "buy_order_id": "7f3c9e10-2b4d-4e6f-8a1b-3c5d7e9f0a12",
    "sell_order_id": "9e8d7c6b-5a4f-4e3d-8c2b-1a0f9e8d7c6b",
    "buyer_trader_id": "3d2a1b0c-9e8f-4a7b-8c6d-5e4f3a2b1c0d",
    "seller_trader_id": "b0b0b0b0-0003-4000-8000-000000000003",
    "aggressor_side": "BUY",
    "buy_order_remaining_quantity": 30,
    "sell_order_remaining_quantity": 0,
    "executed_at": "2026-11-20T10:42:01.263Z"
  }
}
```

### `OrderCancelled`

Remaining quantity was removed from the book: either the owner asked, or a `MARKET` order could not be fully filled
within its protection price.

- **Producer:** `matching-engine`
- **Consumers:** `order-service` (status `CANCELLED`), `portfolio-service` (release leftover reservation),
  `market-data-service` (depth)
- **Key:** `ticker`

```json
{
  "title": "OrderCancelled.data",
  "type": "object",
  "required": ["sequence", "ticker", "order_id", "trader_id", "side", "cancelled_quantity", "reason", "cancelled_at"],
  "properties": {
    "sequence":           { "$ref": "#/$defs/sequence" },
    "ticker":             { "$ref": "#/$defs/ticker" },
    "order_id":           { "$ref": "#/$defs/uuid" },
    "trader_id":          { "$ref": "#/$defs/uuid" },
    "side":               { "$ref": "#/$defs/side" },
    "cancelled_quantity": { "type": "integer", "minimum": 1, "description": "Quantity that was still open and is now removed" },
    "reason":             { "type": "string", "enum": ["USER_REQUESTED", "UNFILLED_MARKET_ORDER"] },
    "cancelled_at":       { "$ref": "#/$defs/timestamp" }
  }
}
```

```json
{
  "event_id": "1c2d3e4f-5a6b-4c7d-8e9f-a0b1c2d3e4f5",
  "event_type": "OrderCancelled",
  "event_version": 1,
  "occurred_at": "2026-11-20T10:43:10.020Z",
  "producer": "matching-engine",
  "environment": "prod",
  "correlation_id": "req-9a8b7c",
  "causation_id": "5a6b7c8d-9e0f-4a1b-8c2d-3e4f5a6b7c8d",
  "data": {
    "sequence": 1077,
    "ticker": "CHAIWALA",
    "order_id": "7f3c9e10-2b4d-4e6f-8a1b-3c5d7e9f0a12",
    "trader_id": "3d2a1b0c-9e8f-4a7b-8c6d-5e4f3a2b1c0d",
    "side": "BUY",
    "cancelled_quantity": 30,
    "reason": "USER_REQUESTED",
    "cancelled_at": "2026-11-20T10:43:10.019Z"
  }
}
```

### `OrderCancelRejected`

A cancel request arrived but there was nothing left to cancel.

- **Producer:** `matching-engine` **Consumers:** `order-service` (clear `cancel_requested`)
- **Key:** `ticker`

```json
{
  "title": "OrderCancelRejected.data",
  "type": "object",
  "required": ["sequence", "ticker", "order_id", "trader_id", "reason", "rejected_at"],
  "properties": {
    "sequence":    { "$ref": "#/$defs/sequence" },
    "ticker":      { "$ref": "#/$defs/ticker" },
    "order_id":    { "$ref": "#/$defs/uuid" },
    "trader_id":   { "$ref": "#/$defs/uuid" },
    "reason":      { "type": "string", "enum": ["ALREADY_FILLED", "ALREADY_CANCELLED", "UNKNOWN_ORDER"] },
    "rejected_at": { "$ref": "#/$defs/timestamp" }
  }
}
```

```json
{
  "event_id": "2d3e4f5a-6b7c-4d8e-9f0a-b1c2d3e4f5a6",
  "event_type": "OrderCancelRejected",
  "event_version": 1,
  "occurred_at": "2026-11-20T10:44:02.511Z",
  "producer": "matching-engine",
  "environment": "prod",
  "correlation_id": "req-1d2e3f",
  "causation_id": "6b7c8d9e-0f1a-4b2c-8d3e-4f5a6b7c8d9e",
  "data": {
    "sequence": 1103,
    "ticker": "CHAIWALA",
    "order_id": "4e5f6a7b-8c9d-4e0f-8a1b-2c3d4e5f6a7b",
    "trader_id": "3d2a1b0c-9e8f-4a7b-8c6d-5e4f3a2b1c0d",
    "reason": "ALREADY_FILLED",
    "rejected_at": "2026-11-20T10:44:02.510Z"
  }
}
```

---

## Topic `candles.v1` (producer: `market-data-service`)

### `CandleClosed`

A candle interval ended. If nothing traded, a flat candle is emitted (open = high = low = close = previous close,
volume 0) so charts and bots have no gaps.

- **Producer:** `market-data-service` **Consumers:** `bot-traders` (policy P-09)
- **Key:** `ticker`
- **Deterministic `event_id`:** UUIDv5 of `ticker|interval|bucket_start`. If several market-data replicas emit the
  same candle, consumers deduplicate it.

```json
{
  "title": "CandleClosed.data",
  "type": "object",
  "required": ["ticker", "interval", "bucket_start", "bucket_end", "open_paise", "high_paise", "low_paise", "close_paise", "volume", "trade_count"],
  "properties": {
    "ticker":       { "$ref": "#/$defs/ticker" },
    "interval":     { "type": "string", "enum": ["5s", "1m", "5m"] },
    "bucket_start": { "$ref": "#/$defs/timestamp" },
    "bucket_end":   { "$ref": "#/$defs/timestamp" },
    "open_paise":   { "$ref": "#/$defs/price_paise" },
    "high_paise":   { "$ref": "#/$defs/price_paise" },
    "low_paise":    { "$ref": "#/$defs/price_paise" },
    "close_paise":  { "$ref": "#/$defs/price_paise" },
    "volume":       { "type": "integer", "minimum": 0 },
    "trade_count":  { "type": "integer", "minimum": 0 }
  }
}
```

```json
{
  "event_id": "8f14e45f-ceea-5e3d-9b3a-6c1c7d1f0a11",
  "event_type": "CandleClosed",
  "event_version": 1,
  "occurred_at": "2026-11-20T10:43:00.002Z",
  "producer": "market-data-service",
  "environment": "prod",
  "correlation_id": "candle-CHAIWALA-1m-2026-11-20T10:42:00Z",
  "causation_id": null,
  "data": {
    "ticker": "CHAIWALA",
    "interval": "1m",
    "bucket_start": "2026-11-20T10:42:00Z",
    "bucket_end": "2026-11-20T10:43:00Z",
    "open_paise": 24500,
    "high_paise": 24620,
    "low_paise": 24480,
    "close_paise": 24550,
    "volume": 1830,
    "trade_count": 74
  }
}
```

---

## Topic `incidents.v1` (producer: `sre-agent`)

All incident events share `incident_id` (format `INC-` + 4 digits) and are keyed by it, so one incident's events
stay in order. Consumer for all: `chaos-service` (builds each experiment's timeline: time to detect, action taken,
time to recover).

### `IncidentOpened`

```json
{
  "title": "IncidentOpened.data",
  "type": "object",
  "required": ["incident_id", "alert_name", "service", "severity", "summary", "opened_at"],
  "properties": {
    "incident_id": { "type": "string", "pattern": "^INC-[0-9]{4}$" },
    "alert_name":  { "type": "string", "description": "Prometheus alert that triggered it" },
    "service":     { "$ref": "#/$defs/service" },
    "severity":    { "type": "string", "enum": ["warning", "critical"] },
    "summary":     { "type": "string" },
    "opened_at":   { "$ref": "#/$defs/timestamp" }
  }
}
```

```json
{
  "event_id": "3e4f5a6b-7c8d-4e9f-8a0b-c1d2e3f4a5b6",
  "event_type": "IncidentOpened",
  "event_version": 1,
  "occurred_at": "2026-11-20T11:05:31.000Z",
  "producer": "sre-agent",
  "environment": "prod",
  "correlation_id": "INC-0007",
  "causation_id": null,
  "data": {
    "incident_id": "INC-0007",
    "alert_name": "OrderServiceHighErrorRate",
    "service": "order-service",
    "severity": "critical",
    "summary": "order-service 5xx ratio is 38% (threshold 5%) for 1 minute",
    "opened_at": "2026-11-20T11:05:31.000Z"
  }
}
```

### `RemediationProposed`

The LLM chose an action and it passed the guardrails. If `requires_approval` is true, it waits for a human.

```json
{
  "title": "RemediationProposed.data",
  "type": "object",
  "required": ["incident_id", "remediation_id", "action", "target_service", "parameters", "explanation", "model", "requires_approval", "proposed_at"],
  "properties": {
    "incident_id":       { "type": "string", "pattern": "^INC-[0-9]{4}$" },
    "remediation_id":    { "$ref": "#/$defs/uuid" },
    "action":            { "$ref": "#/$defs/agent_action" },
    "target_service":    { "oneOf": [{ "$ref": "#/$defs/service" }, { "type": "null" }], "description": "null only for do_nothing" },
    "parameters":        { "type": "object", "properties": { "replicas": { "type": "integer", "minimum": 1, "maximum": 5 }, "to_revision": { "type": "integer", "minimum": 1 } } },
    "explanation":       { "type": "string", "description": "Plain-English reasoning shown on the incident feed" },
    "model":             { "type": "string", "description": "OpenRouter model slug used for the diagnosis" },
    "requires_approval": { "type": "boolean" },
    "proposed_at":       { "$ref": "#/$defs/timestamp" }
  }
}
```

```json
{
  "event_id": "4f5a6b7c-8d9e-4f0a-9b1c-d2e3f4a5b6c7",
  "event_type": "RemediationProposed",
  "event_version": 1,
  "occurred_at": "2026-11-20T11:05:44.120Z",
  "producer": "sre-agent",
  "environment": "prod",
  "correlation_id": "INC-0007",
  "causation_id": "3e4f5a6b-7c8d-4e9f-8a0b-c1d2e3f4a5b6",
  "data": {
    "incident_id": "INC-0007",
    "remediation_id": "d4e5f6a7-b8c9-4d0e-8f1a-2b3c4d5e6f7a",
    "action": "rollback_deployment",
    "target_service": "order-service",
    "parameters": { "to_revision": 7 },
    "explanation": "Errors on order-service jumped from 0.2% to 38% about 40 seconds after revision 8 (release 1.4.1) was deployed. Memory and traffic are normal, and the error logs show a new exception type. A bad release is the most likely cause, so rolling back to revision 7 is the smallest safe fix.",
    "model": "anthropic/claude-sonnet-5.5",
    "requires_approval": true,
    "proposed_at": "2026-11-20T11:05:44.118Z"
  }
}
```

### `RemediationApproved`

```json
{
  "title": "RemediationApproved.data",
  "type": "object",
  "required": ["incident_id", "remediation_id", "decided_by", "decided_at"],
  "properties": {
    "incident_id":    { "type": "string", "pattern": "^INC-[0-9]{4}$" },
    "remediation_id": { "$ref": "#/$defs/uuid" },
    "decided_by":     { "type": "string", "description": "Admin nickname" },
    "comment":        { "type": ["string", "null"] },
    "decided_at":     { "$ref": "#/$defs/timestamp" }
  }
}
```

```json
{
  "event_id": "5a6b7c8d-9e0f-4a1b-8c2d-e3f4a5b6c7d8",
  "event_type": "RemediationApproved",
  "event_version": 1,
  "occurred_at": "2026-11-20T11:05:58.900Z",
  "producer": "sre-agent",
  "environment": "prod",
  "correlation_id": "INC-0007",
  "causation_id": "4f5a6b7c-8d9e-4f0a-9b1c-d2e3f4a5b6c7",
  "data": {
    "incident_id": "INC-0007",
    "remediation_id": "d4e5f6a7-b8c9-4d0e-8f1a-2b3c4d5e6f7a",
    "decided_by": "presenter",
    "comment": "Agreed, roll back.",
    "decided_at": "2026-11-20T11:05:58.899Z"
  }
}
```

### `RemediationDeclined`

Declined either by a human admin or automatically by a guardrail (e.g. cooldown active, target not allowlisted).

```json
{
  "title": "RemediationDeclined.data",
  "type": "object",
  "required": ["incident_id", "remediation_id", "declined_by", "reason", "declined_at"],
  "properties": {
    "incident_id":    { "type": "string", "pattern": "^INC-[0-9]{4}$" },
    "remediation_id": { "$ref": "#/$defs/uuid" },
    "declined_by":    { "type": "string", "enum": ["admin", "guardrail"] },
    "reason":         { "type": "string" },
    "declined_at":    { "$ref": "#/$defs/timestamp" }
  }
}
```

```json
{
  "event_id": "6b7c8d9e-0f1a-4b2c-8d3e-f4a5b6c7d8e9",
  "event_type": "RemediationDeclined",
  "event_version": 1,
  "occurred_at": "2026-11-20T11:20:03.000Z",
  "producer": "sre-agent",
  "environment": "prod",
  "correlation_id": "INC-0008",
  "causation_id": "7c8d9e0f-1a2b-4c3d-8e4f-a5b6c7d8e9f0",
  "data": {
    "incident_id": "INC-0008",
    "remediation_id": "e5f6a7b8-c9d0-4e1f-8a2b-3c4d5e6f7a8b",
    "declined_by": "guardrail",
    "reason": "Cooldown active: order-service was rolled back 4 minutes ago (cooldown is 5 minutes). Escalating to a human.",
    "declined_at": "2026-11-20T11:20:02.998Z"
  }
}
```

### `RemediationExecuted`

```json
{
  "title": "RemediationExecuted.data",
  "type": "object",
  "required": ["incident_id", "remediation_id", "action", "target_service", "parameters", "outcome", "details", "executed_at"],
  "properties": {
    "incident_id":    { "type": "string", "pattern": "^INC-[0-9]{4}$" },
    "remediation_id": { "$ref": "#/$defs/uuid" },
    "action":         { "$ref": "#/$defs/agent_action" },
    "target_service": { "oneOf": [{ "$ref": "#/$defs/service" }, { "type": "null" }] },
    "parameters":     { "type": "object" },
    "outcome":        { "type": "string", "enum": ["SUCCEEDED", "FAILED"] },
    "details":        { "type": "string" },
    "executed_at":    { "$ref": "#/$defs/timestamp" },
    "git_reconciliation": {
      "description": "Only for rollback_deployment (ADR-0016); null for other actions. Optional field added within v1.",
      "oneOf": [
        { "type": "null" },
        {
          "type": "object",
          "required": ["status", "reverted_commit", "pull_request_url", "reason"],
          "properties": {
            "status":           { "type": "string", "enum": ["PR_OPENED", "NOT_NEEDED", "FAILED"] },
            "reverted_commit":  { "type": ["string", "null"], "pattern": "^[0-9a-f]{40}$" },
            "pull_request_url": { "type": ["string", "null"], "format": "uri" },
            "reason":           { "type": "string" }
          }
        }
      ]
    }
  }
}
```

```json
{
  "event_id": "7c8d9e0f-1a2b-4c3d-8e4f-a5b6c7d8e9f0",
  "event_type": "RemediationExecuted",
  "event_version": 1,
  "occurred_at": "2026-11-20T11:06:01.400Z",
  "producer": "sre-agent",
  "environment": "prod",
  "correlation_id": "INC-0007",
  "causation_id": "5a6b7c8d-9e0f-4a1b-8c2d-e3f4a5b6c7d8",
  "data": {
    "incident_id": "INC-0007",
    "remediation_id": "d4e5f6a7-b8c9-4d0e-8f1a-2b3c4d5e6f7a",
    "action": "rollback_deployment",
    "target_service": "order-service",
    "parameters": { "to_revision": 7 },
    "outcome": "SUCCEEDED",
    "details": "Deployment order-service rolled back from revision 8 to revision 7; 2/2 pods ready.",
    "executed_at": "2026-11-20T11:06:01.398Z",
    "git_reconciliation": {
      "status": "NOT_NEEDED",
      "reverted_commit": null,
      "pull_request_url": null,
      "reason": "Application was OutOfSync before the rollback (drift from outside Git); cluster now matches Git."
    }
  }
}
```

### `IncidentResolved`

```json
{
  "title": "IncidentResolved.data",
  "type": "object",
  "required": ["incident_id", "service", "resolution", "opened_at", "resolved_at", "time_to_recover_seconds", "actions_taken"],
  "properties": {
    "incident_id":             { "type": "string", "pattern": "^INC-[0-9]{4}$" },
    "service":                 { "$ref": "#/$defs/service" },
    "resolution":              { "type": "string", "enum": ["RECOVERED", "AUTO_CLEARED", "ESCALATED"], "description": "RECOVERED = cleared after our action; AUTO_CLEARED = cleared with no action; ESCALATED = handed to a human" },
    "opened_at":               { "$ref": "#/$defs/timestamp" },
    "resolved_at":             { "$ref": "#/$defs/timestamp" },
    "time_to_recover_seconds": { "type": "integer", "minimum": 0 },
    "actions_taken":           { "type": "array", "items": { "$ref": "#/$defs/agent_action" } }
  }
}
```

```json
{
  "event_id": "8d9e0f1a-2b3c-4d4e-8f5a-b6c7d8e9f0a1",
  "event_type": "IncidentResolved",
  "event_version": 1,
  "occurred_at": "2026-11-20T11:07:05.000Z",
  "producer": "sre-agent",
  "environment": "prod",
  "correlation_id": "INC-0007",
  "causation_id": "7c8d9e0f-1a2b-4c3d-8e4f-a5b6c7d8e9f0",
  "data": {
    "incident_id": "INC-0007",
    "service": "order-service",
    "resolution": "RECOVERED",
    "opened_at": "2026-11-20T11:05:31.000Z",
    "resolved_at": "2026-11-20T11:07:05.000Z",
    "time_to_recover_seconds": 94,
    "actions_taken": ["rollback_deployment"]
  }
}
```

---

## Local events (not published)

Stored only in the owner's store. Listed so the event-storming picture is complete; their shape may change without
notice.

| Local event | Owner | Stored in | Meaning |
|---|---|---|---|
| `AccountOpened` | `portfolio-service` | Postgres (`accounts` row + audit) | A new trader/bot/load account was created with its starting cash (and shares for bots). |
| `ReservationCreated` | `portfolio-service` | Postgres `reservations` | Cash or shares set aside for an order. |
| `TradeSettled` | `portfolio-service` | Postgres `settlements` | One side of a trade applied to an account. |
| `ReservationReleased` | `portfolio-service` | Postgres `reservations` | Leftover reservation returned (order filled with price improvement, cancelled, or orphaned — no `OrderAccepted` within 120 s). |
| `OrderRejected` | `order-service` | Cassandra `order_events` | Order failed validation or reservation; never reached the bus. |
| `BookSnapshotTaken` | `matching-engine` | Cassandra `book_snapshots` | Full book for a ticker saved at a sequence number. |
| `ChaosExperimentStarted` / `ChaosExperimentEnded` | `chaos-service` | memory + Loki | Experiment lifecycle; `ended` carries outcome `REMEDIATED`, `STOPPED` or `EXPIRED`. |
| `FaultInjected` / `FaultCleared` | `market-data-service` | memory + Loki | Memory-leak fault switched on/off via `/internal/v1/faults`. |

## Summary matrix

| Event | Topic | Producer | order-service | matching-engine | portfolio-service | market-data-service | bot-traders | chaos-service |
|---|---|---|:-:|:-:|:-:|:-:|:-:|:-:|
| `OrderPlaced` | `orders.v1` | order-service | — | ✅ | | | | |
| `OrderCancelRequested` | `orders.v1` | order-service | — | ✅ | | | | |
| `OrderAccepted` | `matching.v1` | matching-engine | ✅ | — | ✅ | ✅ | | |
| `TradeExecuted` | `matching.v1` | matching-engine | ✅ | — | ✅ | ✅ | | |
| `OrderCancelled` | `matching.v1` | matching-engine | ✅ | — | ✅ | ✅ | | |
| `OrderCancelRejected` | `matching.v1` | matching-engine | ✅ | — | | | | |
| `CandleClosed` | `candles.v1` | market-data-service | | | | — | ✅ | |
| `IncidentOpened` | `incidents.v1` | sre-agent | | | | | | ✅ |
| `RemediationProposed` | `incidents.v1` | sre-agent | | | | | | ✅ |
| `RemediationApproved` | `incidents.v1` | sre-agent | | | | | | ✅ |
| `RemediationDeclined` | `incidents.v1` | sre-agent | | | | | | ✅ |
| `RemediationExecuted` | `incidents.v1` | sre-agent | | | | | | ✅ |
| `IncidentResolved` | `incidents.v1` | sre-agent | | | | | | ✅ |

Note what is **absent**: `sre-agent` consumes nothing from `chaos-service`, and nobody consumes `orders.v1` except
the matching engine.

## Viva Notes

**Q: What is in an event envelope and why have one?**
A: Standard fields — `event_id`, `event_type`, `event_version`, `occurred_at`, `producer`, `correlation_id`,
`causation_id` — wrapped around the event-specific `data`. Every consumer can deduplicate, route, trace and version
events the same way.

**Q: How do you guarantee the matching engine processes a cancel after the order it cancels?**
A: Both go to `orders.v1` keyed by ticker, so they are in the same partition, which is strictly ordered.

**Q: What if the same event is delivered twice?**
A: Consumers are idempotent: they record each processed `event_id` (in the same transaction as their change where
possible) and skip repeats.

**Q: How would you make a breaking change to `TradeExecuted`?**
A: Publish version 2 to a new topic `matching.v2` alongside v1, migrate consumers one by one, then retire v1. Adding
an optional field needs no new version because consumers ignore unknown fields.

**Q: Why does `TradeExecuted` carry the remaining quantities?**
A: So consumers can tell when an order is finished without asking anyone: order-service sets `FILLED`, and
portfolio-service releases leftover reserved cash, both from the event alone.
