# sre-agent

> **Status:** Phase 7b — built: the loop, evidence, the model through OpenRouter, the rule-based fallback
> ([ADR-0021](../../docs/adr/0021-rule-based-fallback-when-the-llm-is-unavailable.md)), the guardrails, approval,
> execution through the Kubernetes API, verification, incidents.v1 and every operation of its spec. Follows the layout
> in [coding standards §3](../../docs/05-coding-standards.md#3-folder-layout-inside-every-service) (package
> `sre_agent`).

| | |
|---|---|
| **Owning team** | SRE agent role (enabling), collaborating with the Release & Reliability Team |
| **Owner** | **Kaizer Dewaswala** |
| **Team type** | Enabling ([team topology](../../docs/04-team-topology.md)) |
| **Bounded context** | Reliability Operations (core for this project) |

## Responsibility

Watches Prometheus alerts, opens incidents, collects evidence (metrics, Loki logs, deploy history, pod status), asks an LLM through OpenRouter to choose exactly one of four allowlisted tools (`rollback_deployment`, `scale_deployment` max 5, `restart_deployment`, `do_nothing`), checks guardrails, optionally waits for human approval, executes via the Kubernetes API, verifies recovery and explains every step on the live incident feed. Never reads chaos-service.

## Owned data

Cassandra keyspace `sre_agent`: `incidents` (one JSON document per incident, timeline included), `incidents_by_day`,
`agent_settings`, `action_log`, `incident_counter` ([data ownership](../../docs/03-architecture/02-data-ownership.md)).

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

## How it is built

| Layer | What | Where |
|---|---|---|
| Domain (pure, no I/O) | The four tools and their targets, guardrails G1–G10, the fallback's rules, the fixed declines, revisions (a release is its pod template without the restart annotation), evidence and its scrubbing, the tool menu, the prompt, the incident aggregate | `domain/` |
| Application | The loop (`agent.py`: one task per incident: evidence → decide → guard → approve → act → verify), the evidence collector (the PromQL, Loki and Kubernetes questions, and the three facts of ADR-0021), the decider (model, or rules), the feed hub | `application/` |
| Adapters | Prometheus and Loki (HTTP), Kubernetes (HTTPS with the ServiceAccount token: GET and PATCH only), OpenRouter, Cassandra, incidents.v1 | `adapters/` |

**Configuration** (environment variables, [config.py](src/sre_agent/config.py)): `PROMETHEUS_URL` and `LOKI_URL`
(no Prometheus, as in Docker Compose: the loop does not run, the API does), `OPENROUTER_API_KEY` (no key: the rules
decide), `SRE_AGENT_MODEL` (default `anthropic/claude-sonnet-5.5`), `SRE_AGENT_FALLBACK_MODELS`,
`APPROVAL_REQUIRED_BY_DEFAULT` (unset: on in prod, off elsewhere; A-14 changes it at runtime), `POLL_SECONDS` (5).

**Tests:** unit tests run the real collector, decider, guardrails and incident against a fake cluster on a virtual
clock (`tests/fakes.py`): a 4-minute verification takes milliseconds. Integration tests use a real Cassandra 5.0.9
(`make test-integration s=sre-agent`). Every event is checked against its catalog schema.

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
A: Anything other than one tool call with JSON arguments is unusable: fixed rules then choose from the error type,
whether the running release is new, and the load ([ADR-0021](../../docs/adr/0021-rule-based-fallback-when-the-llm-is-unavailable.md)),
through the same guardrails, labelled "rule-based (LLM unavailable)". A tool call with bad arguments is declined by
the guardrails.

**Q: Why can't it see chaos-service?**
A: So its diagnosis is genuine — it must infer causes from symptoms like a human SRE.
