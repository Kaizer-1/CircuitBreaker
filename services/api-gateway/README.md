# api-gateway

> **Status:** Phase 0 — design only, no code yet. This folder will follow the layout in
> [coding standards §3](../../docs/05-coding-standards.md#3-folder-layout-inside-every-service)
> (Python package `api_gateway`).

| | |
|---|---|
| **Owning team** | Platform & Reliability Team |
| **Owner** | **Mohammad Oweis** |
| **Team type** | Platform ([team topology](../../docs/04-team-topology.md)) |
| **Bounded context** | Access (generic subdomain) |

## Responsibility

The single public entry point. Issues and verifies JWT sessions (trader, bot, admin), checks roles, applies rate limits (20 req/s per trader, 50 req/s per bot token), adds `X-Correlation-Id`, strips client-supplied identity headers and sets `X-Trader-Id` / `X-Role` / `X-Nickname`, proxies HTTP and WebSocket traffic to the owning service, and refuses every `/internal/` path.

## Owned data

None (stateless). Sessions are self-contained signed JWTs (`JWT_SECRET`).

## Events

| | |
|---|---|
| **Produces** | — |
| **Consumes** | — |

Schemas: [event catalog](../../docs/03-architecture/03-event-catalog.md).

## API

A-01 `POST /v1/sessions`, A-03 `POST /v1/bot-sessions`, A-04 `POST /v1/admin/sessions`, Q-01 `GET /v1/session`; proxies every other public route and the WebSockets `/v1/ws/*`. Spec: [`api/api-gateway.yaml`](../../api/api-gateway.yaml).

Every service also exposes `/healthz`, `/readyz`, `/metrics` ([coding standards §7](../../docs/05-coding-standards.md#7-standard-operational-endpoints)).

## Synchronous dependencies

`portfolio-service` `POST /internal/v1/accounts` (A-02) when someone joins.

## Scaling and operations

Default 2 replicas, agent may scale 1–5. Stateless, so any replica can serve any request.

## Viva Notes

**Q: Why put authentication in the gateway?**
A: One place to verify tokens and roles, so no service re-implements security; services trust identity headers only from the gateway (network policies).

**Q: Why does the gateway strip `X-Trader-Id` from clients?**
A: Otherwise a client could pretend to be someone else. Only the gateway, after verifying the JWT, may set it.

**Q: Is the gateway a single point of failure?**
A: It runs at least 2 replicas behind a Kubernetes Service, is stateless, and the agent can scale or restart it.
