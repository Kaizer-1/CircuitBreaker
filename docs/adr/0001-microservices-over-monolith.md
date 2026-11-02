# 0001 — Microservices over a Monolith

- **Status:** Accepted
- **Date:** 2026-11-02
- **Deciders:** Kaizer Dewaswala, MD Asif Ali, Mohammad Oweis

## Context

CircuitBreaker must (a) demonstrate every microservices topic in the syllabus, (b) let three people work in parallel
with little coordination, (c) let parts fail and recover independently for the chaos demo, and (d) scale parts
differently (stateless order intake vs single-writer matching engine). The domain has clear bounded contexts found by
event storming ([01-event-storming.md](../02-domain/01-event-storming.md)).

## Decision

Build the system as **8 independently deployable services** plus a web frontend, one per bounded context, each owning
its data and communicating through versioned HTTP APIs and events. Sizing is justified in
[03-service-sizing.md](../02-domain/03-service-sizing.md).

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| **Monolith** (one FastAPI app, one database) | Simplest to build, but cannot show independent deployment, data ownership, failure isolation or per-service scaling — the core of the course and of the self-healing demo. A memory leak would take down everything. |
| **Modular monolith** (one deployable, strict internal modules) | A strong real-world choice and the usual "start here" advice. Rejected because the chaos and self-healing story needs separate processes to fail, scale, roll back and restart independently. |
| **Many fine-grained services (20+)** | Too much operational cost for 3 people; many lock-step deploys (see sizing doc). |

## Consequences

- ✅ Each team deploys and scales its services independently; failures are isolated (memory-leak scenario).
- ✅ Direct coverage of the syllabus: service boundaries, data ownership, event sourcing, CQRS, Kubernetes.
- ❌ Distributed-systems complexity: network failures, eventual consistency, duplicate messages, more infrastructure.
- ❌ Harder local development — mitigated by Docker Compose and a shared Helm sub-chart.
- ❌ Requires good observability to debug — addressed in ADR-0008.

## Viva Notes

**Q: Isn't "monolith first" the usual advice?**
A: Yes, when the domain is unclear. Here event storming gave us clear boundaries up front, and the course and the
self-healing demo specifically need independent deployment and failure isolation.

**Q: What does microservices cost you?**
A: Network failures, eventual consistency, duplicate messages to handle, more infrastructure and harder debugging. We
pay for it with idempotent consumers, observability and automation.

**Q: How would you know if microservices were the wrong choice?**
A: If most features required changing several services at once, or deploys had to be coordinated — signs of a
distributed monolith. We track this with the transformation metrics in the roadmap.
