# chaos-service

> **Status:** Phase 0 — design only, no code yet. This folder will follow the layout in
> [coding standards §3](../../docs/05-coding-standards.md#3-folder-layout-inside-every-service)
> (Python package `chaos_service`).

| | |
|---|---|
| **Owning team** | Platform & Reliability Team |
| **Owner** | **Mohammad Oweis** |
| **Team type** | Enabling ([team topology](../../docs/04-team-topology.md)) |
| **Bounded context** | Chaos Engineering (supporting) |

## Responsibility

Runs one chaos experiment at a time with an automatic expiry and an explicit undo: `BAD_DEPLOY` (new order-service revision with `FAULT_PROFILE=bad-release`), `MEMORY_LEAK` (fault hook in market-data-service), `TRAFFIC_SPIKE` (built-in load generator using 10 `LOAD` accounts through the gateway). Measures time to detect and time to recover from `incidents.v1`.

## Owned data

In memory (experiments are demo-session data), also logged to Loki.

## Events

| | |
|---|---|
| **Produces** | — on the bus. Local: `ChaosExperimentStarted`, `ChaosExperimentEnded`. |
| **Consumes** | `IncidentOpened`, `RemediationProposed`, `RemediationApproved`, `RemediationDeclined`, `RemediationExecuted`, `IncidentResolved` ← `incidents.v1`. |

Schemas: [event catalog](../../docs/03-architecture/03-event-catalog.md).

## API

Q-21 `GET /v1/chaos/scenarios`, A-10 `POST /v1/chaos/experiments`, Q-22 `GET /v1/chaos/experiments`, Q-23 `GET /v1/chaos/experiments/{experiment_id}`, A-11 `POST /v1/chaos/experiments/{experiment_id}/stop`. All admin. Spec: [`api/chaos-service.yaml`](../../api/chaos-service.yaml).

Every service also exposes `/healthz`, `/readyz`, `/metrics` ([coding standards §7](../../docs/05-coding-standards.md#7-standard-operational-endpoints)).

## Synchronous dependencies

Kubernetes API (patch the order-service Deployment), `market-data-service` `/internal/v1/faults` (A-12, A-13), `api-gateway` public API (load generator).

## Scaling and operations

**Exactly 1 replica.** Never targets data stores.

## Viva Notes

**Q: What makes a chaos experiment safe?**
A: One at a time, bounded duration, explicit undo, small blast radius (one service, never data stores), and a stop button.

**Q: How do you measure the agent?**
A: Time to detect (start → IncidentOpened) and time to recover (start → IncidentResolved), read from incidents.v1.

**Q: Does chaos-service tell the agent what it broke?**
A: Never. Information flows only from the agent to chaos-service, not back.
