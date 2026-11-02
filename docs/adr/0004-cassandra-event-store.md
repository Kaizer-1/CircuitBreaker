# 0004 — Cassandra as the Event Store

- **Status:** Accepted
- **Date:** 2026-11-02
- **Deciders:** Kaizer Dewaswala, MD Asif Ali, Mohammad Oweis

## Context

The matching engine stores every market event forever, append-only, and reads them back by **ticker and sequence
range** (replay, time travel, re-publishing). Writes are frequent; updates and deletes never happen. Other services
(order-service, market-data-service, sre-agent) also have append-heavy, time-ordered data with simple, known queries.
Cassandra is also a syllabus topic (Unit IV).

## Decision

Use **Apache Cassandra** as the event store (keyspace `matching_engine`, table `market_events` partitioned by
`(ticker, day)` and clustered by `sequence`), and for the other append-heavy stores (`order_service`, `market_data`,
`sre_agent`). One keyspace and one role per service; one cluster per environment. Table designs:
[02-data-ownership.md](../03-architecture/02-data-ownership.md).

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| **Postgres for events** | Works well at our scale and offers transactions. Rejected for the event store because the access pattern (append-only, range by partition key) is Cassandra's sweet spot, and Cassandra is in the syllabus. Postgres *is* used where transactions matter (ADR-0006). |
| **EventStoreDB** (purpose-built event store) | Excellent fit for event sourcing, but adds another niche technology outside the syllabus. |
| **Redpanda as the event store** (infinite retention) | Tempting, but a broker is not a database: no queries by time, harder snapshots, and retention/compaction settings make it easy to lose history. |
| **MongoDB** | General-purpose; no advantage over Cassandra for time-ordered appends; not in the syllabus. |

## Consequences

- ✅ Fast appends; efficient range reads within a `(ticker, day)` partition; linear scalability if ever needed.
- ✅ Logged batches give all-or-nothing appends for one command's events.
- ❌ No multi-partition transactions or joins — we model one table per query and accept duplication.
- ❌ Query-first modelling is unfamiliar; tables must be designed around access patterns up front.
- ❌ Cassandra is memory-hungry for a laptop; we run a single node with reduced heap in dev.

## Viva Notes

**Q: What are the partition key and clustering key of `market_events`, and why?**
A: Partition key `(ticker, day)` keeps one ticker's events for one day together on one node and stops partitions
growing forever; clustering key `sequence` keeps them sorted so a replay is one ordered range read.

**Q: Cassandra has no transactions — how do you append several events atomically?**
A: All events from one command are in the same partition, and a logged batch within a single partition is applied
atomically.

**Q: Why not just use Postgres everywhere?**
A: Postgres would work at our scale. We chose stores by access pattern: Cassandra for append-only time series and
event logs, Postgres where we need multi-row transactions and constraints (portfolio).
