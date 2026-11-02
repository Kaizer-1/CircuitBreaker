# 0003 — Redpanda as the Event Bus

- **Status:** Accepted
- **Date:** 2026-11-02
- **Deciders:** Kaizer Dewaswala, MD Asif Ali, Mohammad Oweis

## Context

Services must react to each other's facts asynchronously (`OrderPlaced`, `TradeExecuted`, `CandleClosed`,
`IncidentResolved`). We need **ordering per ticker**, **replay** of recent messages for consumers that restart,
**consumer groups** so each service gets every event, and it must run on a laptop and in kind.

## Decision

Use **Redpanda**, a Kafka-API-compatible streaming platform, with topics `orders.v1`, `matching.v1`, `candles.v1`,
`incidents.v1` ([event catalog](../03-architecture/03-event-catalog.md)), keyed by ticker (or incident ID), with
at-least-once delivery and idempotent consumers. Services use standard Kafka client libraries.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| **Apache Kafka** | Same model and API; heavier to run locally (JVM, more memory). Because Redpanda speaks the Kafka protocol, switching later needs no code change. |
| **RabbitMQ** | Excellent queue, but messages are removed once consumed: no built-in replay log, and per-key ordering across consumers is harder. |
| **Redis Streams** | Lightweight, but weaker durability guarantees and less standard tooling for consumer groups at scale. |
| **Direct HTTP calls between services** | Couples availability (a slow consumer slows the producer) and needs the producer to know every consumer. |
| **Cloud queues (SQS/SNS)** | No real cloud by choice; FIFO ordering limits; LocalStack emulation is not the same as a real broker. |

## Consequences

- ✅ Per-partition ordering + ticker keys guarantee the engine sees each ticker's orders and cancels in order.
- ✅ Retained log lets consumers catch up after downtime (memory-leak restart).
- ✅ Single binary, low memory — fits a laptop and kind.
- ❌ At-least-once delivery means duplicates; every consumer must be idempotent.
- ❌ One more stateful component to operate; retention is only 7 days, so the permanent history lives in Cassandra.

## Viva Notes

**Q: Why Redpanda instead of Kafka?**
A: Same API and semantics, much lighter to run (one binary, no JVM). Our code uses standard Kafka clients, so we could
switch to Kafka without changing services.

**Q: How do you guarantee ordering?**
A: Kafka-style brokers order messages within a partition. We key order messages by ticker, so all of one ticker's
messages are in one partition and are consumed in order.

**Q: Why not RabbitMQ?**
A: We need a replayable log and per-key ordering for many independent consumer groups; that is what log-based brokers
do best.
