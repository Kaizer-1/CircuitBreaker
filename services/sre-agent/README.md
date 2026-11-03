# sre-agent

> **Status:** Phase 1 — runnable skeleton. Follows the layout in
> [coding standards §3](../../docs/05-coding-standards.md#3-folder-layout-inside-every-service)
> (Python package `sre_agent`). Every operation in its API spec answers `501 Not Implemented` until its phase;
> `/healthz`, `/readyz` and `/metrics` work.

| | |
|---|---|
| **Owning team** | SRE agent role (enabling), collaborating with the Release & Reliability Team |
| **Owner** | **Kaizer Dewaswala** |
| **Team type** | Enabling ([team topology](../../docs/04-team-topology.md)) |
| **Bounded context** | Reliability Operations (core for this project) |

## Responsibility

Watches Prometheus alerts, opens incidents, collects evidence (metrics, Loki logs, deploy history, pod status), asks an LLM through OpenRouter to choose exactly one of four allowlisted tools (`rollback_deployment`, `scale_deployment` max 5, `restart_deployment`, `do_nothing`), checks guardrails, optionally waits for human approval, executes via the Kubernetes API, verifies recovery and explains every step on the live incident feed. Never reads chaos-service.

## Owned data

Cassandra keyspace `sre_agent`: `incidents`, `incidents_by_day`, `incident_timeline`, `agent_settings`, `action_log`.

## Events

| | |
|---|---|
| **Produces** | `IncidentOpened`, `RemediationProposed`, `RemediationApproved`, `RemediationDeclined`, `RemediationExecuted`, `IncidentResolved` → `incidents.v1`. |
| **Consumes** | — from the bus. Reads Prometheus, Loki and the Kubernetes API. |

Schemas: [event catalog](../../docs/03-architecture/03-event-catalog.md).

## API

Q-17 `GET /v1/incidents`, Q-18 `GET /v1/incidents/{incident_id}`, A-15 `POST /v1/incidents/{incident_id}/decision`, Q-20 `GET /v1/agent/settings`, A-14 `PUT /v1/agent/settings`, WebSocket Q-19 `/v1/ws/incidents`. Spec: [`api/sre-agent.yaml`](../../api/sre-agent.yaml). A-16 and A-17 run in the agent loop.

Every service also exposes `/healthz`, `/readyz`, `/metrics` ([coding standards §7](../../docs/05-coding-standards.md#7-standard-operational-endpoints)).

## Synchronous dependencies

Prometheus HTTP API, Loki HTTP API, Kubernetes API (ServiceAccount `sre-agent`, minimal Role), OpenRouter chat-completions API.

## Scaling and operations

**Exactly 1 replica** (one decision-maker). **Never give this service tools beyond the four in the allowlist** ([ADR-0009](../../docs/adr/0009-sre-agent-fixed-tool-allowlist.md)).

## Run and test it alone

```bash
cd services/sre-agent
cp .env.example .env                 # local settings (git-ignored)
uv sync                              # create the virtual environment from uv.lock
uv run pytest                        # unit, smoke and contract tests (contract test reads ../../api/)
uv run ruff check . && uv run ruff format --check . && uv run mypy

make infra                           # from the repo root: Redpanda, Cassandra, Postgres and their init jobs
uv run uvicorn sre_agent.main:create_app --factory --reload --port 8006
curl localhost:8006/readyz         # or `make up` from the repo root to run its container
```

**Ready means:**

- Redpanda, and the topic(s) `incidents.v1` must exist (the init job creates them)
- Cassandra: logs in with its own role `sre_agent_role` and uses keyspace `sre_agent` only

## Viva Notes

**Q: What can the agent do?**
A: Exactly four things: roll back, scale (1–5 replicas), restart, or do nothing — each checked by guardrails and limited by RBAC.

**Q: What if the LLM is down or answers nonsense?**
A: Anything other than one valid tool call becomes do_nothing plus an escalation to the admin.

**Q: Why can't it see chaos-service?**
A: So its diagnosis is genuine — it must infer causes from symptoms like a human SRE.
