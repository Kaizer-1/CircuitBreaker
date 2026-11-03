# chaos-service

> **Status:** Phase 1 — runnable skeleton. Follows the layout in
> [coding standards §3](../../docs/05-coding-standards.md#3-folder-layout-inside-every-service)
> (Python package `chaos_service`). Every operation in its API spec answers `501 Not Implemented` until its phase;
> `/healthz`, `/readyz` and `/metrics` work.

| | |
|---|---|
| **Owning team** | Release & Reliability Team (based on the textbook's release team) |
| **Owner** | **Mohammad Oweis** |
| **Team type** | Complicated-subsystem ([team topology](../../docs/04-team-topology.md)) |
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

## Run and test it alone

```bash
cd services/chaos-service
cp .env.example .env                 # local settings (git-ignored)
uv sync                              # create the virtual environment from uv.lock
uv run pytest                        # unit, smoke and contract tests (contract test reads ../../api/)
uv run ruff check . && uv run ruff format --check . && uv run mypy

make infra                           # from the repo root: Redpanda, Cassandra, Postgres and their init jobs
uv run uvicorn chaos_service.main:create_app --factory --reload --port 8007
curl localhost:8007/readyz         # or `make up` from the repo root to run its container
```

**Ready means:**

- Redpanda, and the topic(s) `incidents.v1` must exist (the init job creates them)

## Viva Notes

**Q: What makes a chaos experiment safe?**
A: One at a time, bounded duration, explicit undo, small blast radius (one service, never data stores), and a stop button.

**Q: How do you measure the agent?**
A: Time to detect (start → IncidentOpened) and time to recover (start → IncidentResolved), read from incidents.v1.

**Q: Does chaos-service tell the agent what it broke?**
A: Never. Information flows only from the agent to chaos-service, not back.
