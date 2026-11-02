# 0006 — CQRS with a Postgres Portfolio Read Model

- **Status:** Accepted
- **Date:** 2026-11-02
- **Deciders:** MD Asif Ali (owner), Kaizer Dewaswala, Mohammad Oweis

## Context

Traders constantly ask "what do I own and how am I doing?" and everyone watches the leaderboard. Answering these by
replaying the market event store on every request would be slow. Balances must also be **exactly right**: settlement
changes buyer cash, buyer holding, seller cash, seller holding and a reservation together, and cash must never go
negative.

## Decision

Apply **CQRS**: the write side is order-service + matching-engine (event-sourced); `portfolio-service` maintains a
**read model in Postgres** (accounts, holdings, reservations, settlements, last prices) by consuming `matching.v1`.
Each event is applied in **one SQL transaction** together with an insert into `processed_events` (idempotency), with
`CHECK` constraints enforcing non-negative balances. portfolio-service also owns the `Account` aggregate for
reservations (ADR-0011), so it is a read model *plus* a small write model for money — stated honestly.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| **Compute portfolios on demand from the event store** | Correct but slow and expensive per request; leaderboard would replay everything every 2 s. |
| **Cassandra for portfolios** | No multi-row transactions or CHECK constraints; settlement would need complex compensation. |
| **Portfolio inside matching-engine** | Mixes money with matching; the engine would grow and become harder to keep single-writer and fast. |
| **Redis cache** | Fast but not a durable source of truth for money; no constraints. |

## Consequences

- ✅ Fast queries (simple SQL), transactional correctness, constraints as a safety net.
- ✅ Fully rebuildable from the market event store if corrupted.
- ✅ Different technology for a different job — shows polyglot persistence.
- ❌ Eventual consistency: a fill appears in the portfolio slightly after the trade.
- ❌ One more database to operate.
- ❌ Not a "pure" read model, because reservations are commands — documented in ADR-0011.

## Viva Notes

**Q: What does CQRS mean in your portfolio service?**
A: Writes (orders and matching) happen elsewhere and emit events; portfolio-service listens and keeps a model shaped
for reading portfolios and the leaderboard.

**Q: Why Postgres here when you used Cassandra elsewhere?**
A: Settlement must update five rows atomically and never let cash go negative. That is what ACID transactions and
CHECK constraints are for.

**Q: How do you avoid applying a trade twice?**
A: In the same transaction as the settlement, insert the event ID into `processed_events` (primary key). A duplicate
fails the insert, the transaction rolls back, and the duplicate is skipped.
