# chaos-service

> **Status:** Phase 7 — built: four scenarios, stop, reset, expiry and the agent's timeline. Follows the layout in
> [coding standards §3](../../docs/05-coding-standards.md#3-folder-layout-inside-every-service)
> (Python package `chaos_service`).

| | |
|---|---|
| **Owning team** | Release & Reliability Team (based on the textbook's release team) |
| **Owner** | **Mohammad Oweis** |
| **Team type** | Complicated-subsystem ([team topology](../../docs/04-team-topology.md)) |
| **Bounded context** | Chaos Engineering (supporting) |

## Responsibility

Runs chaos experiments with an automatic expiry and an explicit undo, and measures how the SRE agent copes
([chaos scenarios](../../docs/03-architecture/06-chaos-scenarios.md), [ADR-0020](../../docs/adr/0020-chaos-triggers-through-each-targets-own-hook.md)).
Every fault lives in the service it breaks; chaos-service only switches it:

| Scenario (Chaos Panel button) | How it is switched on | Undo |
|---|---|---|
| `BAD_DEPLOY` Bad deploy | Patches the order-service Deployment: `FAULT_PROFILE=bad-release` (+ `FAULT_ERROR_RATE`), change-cause "release 1.4.1": a real new rollout revision | Patch them out: the previous pod template returns |
| `MEMORY_LEAK` Memory leak | market-data-service A-12 (`rate_mb_per_minute`, default 80) | A-13 (gives the memory back) |
| `TRAFFIC_SPIKE` Panic sell | bot-traders A-23 (`requests_per_second`, default 110, for the experiment's duration) | A-24 |
| `PORTFOLIO_DB_DEGRADED` Portfolio database degraded | portfolio-service A-21 (`delay_ms`, default 1500) | A-22 |

**Overlap:** each scenario lists the services whose alerts it makes fire (`affects`: the leak only market-data-service;
the bad deploy, the panic and the database fault all order-service). Experiments may run together only if they
affect different services, so every incident still has one cause; otherwise `409 EXPERIMENT_ALREADY_RUNNING`.
**Reset (A-20)** ends every experiment and undoes every fault by checking what is actually running (it works even
after chaos-service restarted), and returns order-service to the replicas it had before the last spike (or 2).
**Expiry:** an experiment undoes itself after its duration. **Remediated:** when the agent's incident on the
scenario's primary service is resolved `RECOVERED`, the experiment ends `REMEDIATED` and anything left is undone; an
`ESCALATED` incident (the database fault: the agent rightly does nothing) leaves it running for a person.

## Owned data

In memory (experiments are demo-session data), also logged to Loki.

## Events

| | |
|---|---|
| **Produces** | — on the bus. Local: `ChaosExperimentStarted`, `ChaosExperimentEnded`. |
| **Consumes** | `IncidentOpened`, `RemediationProposed`, `RemediationApproved`, `RemediationDeclined`, `RemediationExecuted`, `IncidentResolved` ← `incidents.v1`. |

Schemas: [event catalog](../../docs/03-architecture/03-event-catalog.md).

## API

Q-21 `GET /v1/chaos/scenarios`, A-10 `POST /v1/chaos/experiments`, Q-22 `GET /v1/chaos/experiments` (with the
resets), Q-23 `GET /v1/chaos/experiments/{experiment_id}`, A-11 `POST /v1/chaos/experiments/{experiment_id}/stop`,
A-20 `POST /v1/chaos/reset`. All admin (the gateway checks, and so does the service from `X-Role`). Spec:
[`api/chaos-service.yaml`](../../api/chaos-service.yaml).

**Metrics** (for the Incident view's annotations and MTTD; the SRE agent's evidence never reads them):
`chaos_experiments_started_total{scenario}`, `chaos_experiments_ended_total{scenario,outcome}`,
`chaos_experiment_running{scenario}`, `chaos_time_to_detect_seconds{scenario}`,
`chaos_time_to_recover_seconds{scenario}` (histograms), `chaos_resets_total`.

Every service also exposes `/healthz`, `/readyz`, `/metrics` ([coding standards §7](../../docs/05-coding-standards.md#7-standard-operational-endpoints)).

## Synchronous dependencies

Kubernetes API with its own ServiceAccount, whose Role names one object: `get`/`patch` on `deployments/order-service`
and its `scale` (in its own namespace). The hooks: market-data-service A-12/A-13, portfolio-service A-21/A-22,
bot-traders A-23/A-24 (`/internal/v1/faults`; only chaos-service may call them, by NetworkPolicy). Outside Kubernetes
(Docker Compose) the bad deploy and the replica reset are unavailable (`503`).

## Scaling and operations

**Exactly 1 replica** (experiments are in its memory). Never targets data stores: its Role cannot name a
StatefulSet, and the database fault is portfolio-service's own hook.

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
A: One experiment per affected service, a bounded duration, an explicit undo, a reset that checks what is really
running, a small blast radius (its Kubernetes permission names one Deployment; never a data store) and a stop button.

**Q: How do you measure the agent?**
A: Time to detect (start → IncidentOpened) and time to recover (start → IncidentResolved), read from incidents.v1.

**Q: Does chaos-service tell the agent what it broke?**
A: Never. Information flows only from the agent to chaos-service, not back.
