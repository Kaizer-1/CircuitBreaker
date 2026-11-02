# sre-agent

> **Status:** Phase 0 — design only, no code yet. This folder will follow the layout in
> [coding standards §3](../../docs/05-coding-standards.md#3-folder-layout-inside-every-service)
> (Python package `sre_agent`).

| | |
|---|---|
| **Owning team** | Kaizer Dewaswala (enabling role, collaborating with the Platform & Reliability Team) |
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

## Viva Notes

**Q: What can the agent do?**
A: Exactly four things: roll back, scale (1–5 replicas), restart, or do nothing — each checked by guardrails and limited by RBAC.

**Q: What if the LLM is down or answers nonsense?**
A: Anything other than one valid tool call becomes do_nothing plus an escalation to the admin.

**Q: Why can't it see chaos-service?**
A: So its diagnosis is genuine — it must infer causes from symptoms like a human SRE.
