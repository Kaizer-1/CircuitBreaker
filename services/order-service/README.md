# order-service

> **Status:** Phase 0 — design only, no code yet. This folder will follow the layout in
> [coding standards §3](../../docs/05-coding-standards.md#3-folder-layout-inside-every-service)
> (Python package `order_service`).

| | |
|---|---|
| **Owning team** | Trading Team |
| **Owner** | **Kaizer Dewaswala** |
| **Team type** | Stream-aligned ([team topology](../../docs/04-team-topology.md)) |
| **Bounded context** | Order Management (core) |

## Responsibility

Accepts LIMIT and MARKET orders, validates ticker, quantity and the ₹0.05 tick size, reserves cash or shares synchronously via portfolio-service (no margin, no short selling), stores order events with an outbox, publishes `OrderPlaced` / `OrderCancelRequested`, and keeps each order's status current from matching events.

## Owned data

Cassandra keyspace `order_service`: `orders_by_id`, `orders_by_trader`, `order_events` (with outbox flag), `processed_events`.

## Events

| | |
|---|---|
| **Produces** | `OrderPlaced`, `OrderCancelRequested` → `orders.v1`. Local: `OrderRejected`. |
| **Consumes** | `OrderAccepted`, `TradeExecuted`, `OrderCancelled`, `OrderCancelRejected` ← `matching.v1`. |

Schemas: [event catalog](../../docs/03-architecture/03-event-catalog.md).

## API

A-05 `POST /v1/orders`, A-07 `DELETE /v1/orders/{order_id}`, Q-07 `GET /v1/orders`, Q-08 `GET /v1/orders/{order_id}`. Spec: [`api/order-service.yaml`](../../api/order-service.yaml).

Every service also exposes `/healthz`, `/readyz`, `/metrics` ([coding standards §7](../../docs/05-coding-standards.md#7-standard-operational-endpoints)).

## Synchronous dependencies

`portfolio-service` `POST /internal/v1/reservations` (A-06), 500 ms timeout, circuit breaker.

## Scaling and operations

Default 2 replicas, agent may scale 1–5 (traffic-spike target). Concurrency limit 50 in-flight requests per pod. Chaos target for `BAD_DEPLOY` and `TRAFFIC_SPIKE`.

## Viva Notes

**Q: Why does placing an order return 202?**
A: Matching is asynchronous; the order is accepted for processing and its status changes later via events.

**Q: What is the outbox for?**
A: To make 'save the order' and 'publish OrderPlaced' reliable without a distributed transaction: unpublished events are re-sent by a relay (but never after 60 s — see ADR-0011).

**Q: How do you stop overspending?**
A: A synchronous reservation in portfolio-service before publishing; it fails with 422 if cash or shares are insufficient.
