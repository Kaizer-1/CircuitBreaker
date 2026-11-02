# 0005 — Event Sourcing for the Matching Engine

- **Status:** Accepted
- **Date:** 2026-11-02
- **Deciders:** Kaizer Dewaswala (owner), MD Asif Ali, Mohammad Oweis

## Context

Key requirements: **time travel** (rebuild the market at any past moment), **replay** (play the past at high speed),
a complete **audit trail** (a fair exchange must prove what happened), **crash recovery** of in-memory order books,
and a natural feed of facts for read models (CQRS).

## Decision

The matching engine is **event sourced**. For each command it *decides* (matches) and produces events
(`OrderAccepted`, `TradeExecuted`, `OrderCancelled`, `OrderCancelRejected`) with per-ticker sequence numbers, appends
them to Cassandra, *applies* them to the in-memory book, then publishes them. State = replay of events. Snapshots every
500 events per ticker bound replay time. Replay and time travel only *apply* events; they never re-run matching.
Details: [04-event-sourcing-and-cqrs.md](../03-architecture/04-event-sourcing-and-cqrs.md).

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| **Store current state only** (book table, trades table) | Cannot rebuild the book at an arbitrary past time; audit trail incomplete (cancellations/modifications lost). |
| **Current state + audit log table** | Two sources of truth that can disagree; time travel still needs to reconstruct the book from the log anyway. |
| **Event-source every service** | Adds snapshot/versioning/replay complexity where queries are simple; we use it only where it pays off. |

## Consequences

- ✅ Time travel and replay are natural; full audit trail; crash recovery = snapshot + replay.
- ✅ Read models (candles, portfolios, order status) can be rebuilt from history at any time.
- ✅ Bugs in projections are fixable after the fact by replaying.
- ❌ Events are permanent contracts; changing their shape needs versioning (event catalog rules).
- ❌ Requires snapshots, sequence management and careful "store then publish" logic.
- ❌ Requires a single writer per ticker (one engine replica) — see ADR-0007 limits and the sizing doc.

## Viva Notes

**Q: What exactly is stored and how is the book rebuilt?**
A: Every `OrderAccepted`, `TradeExecuted`, `OrderCancelled`, `OrderCancelRejected` with a sequence number. To rebuild,
load the nearest snapshot and apply later events in sequence order.

**Q: What is the difference between deciding and applying?**
A: Deciding runs the business rules (matching) and produces events; applying just changes state according to an
event. Replay only applies, so history is reproduced exactly even if matching code changes later.

**Q: What are the downsides of event sourcing?**
A: Event schemas become permanent contracts, you need snapshots and replay tooling, and it's harder to understand than
CRUD. That's why we use it only in the matching engine.
