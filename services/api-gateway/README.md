# api-gateway

> **Status:** Phase 1 — runnable skeleton. Follows the layout in
> [coding standards §3](../../docs/05-coding-standards.md#3-folder-layout-inside-every-service)
> (Python package `api_gateway`). Every operation in its API spec answers `501 Not Implemented` until its phase;
> `/healthz`, `/readyz` and `/metrics` work.

| | |
|---|---|
| **Owning team** | API Team (the textbook's consumer-facing API team: owns the APIs at the system boundary) |
| **Owner** | **MD Asif Ali** |
| **Team type** | Stream-aligned ([team topology](../../docs/04-team-topology.md)) |
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

## Run and test it alone

```bash
cd services/api-gateway
cp .env.example .env                 # local settings (git-ignored)
uv sync                              # create the virtual environment from uv.lock
uv run pytest                        # unit, smoke and contract tests (contract test reads ../../api/)
uv run ruff check . && uv run ruff format --check . && uv run mypy

make infra                           # from the repo root: Redpanda, Cassandra, Postgres and their init jobs
uv run uvicorn api_gateway.main:create_app --factory --reload --port 8080
curl localhost:8080/readyz         # or `make up` from the repo root to run its container
```

**Ready means:**

- none of its own: it needs no database or broker. `/readyz` is `200` once it is configured and running. `GET /v1/system/status` asks every other service's `/readyz`.

The gateway reads `SERVICE_UPSTREAMS` (a JSON map of service name to base URL; the defaults are the host ports of `make up`).

## Viva Notes

**Q: Why put authentication in the gateway?**
A: One place to verify tokens and roles, so no service re-implements security; services trust identity headers only from the gateway (network policies).

**Q: Why does the gateway strip `X-Trader-Id` from clients?**
A: Otherwise a client could pretend to be someone else. Only the gateway, after verifying the JWT, may set it.

**Q: Is the gateway a single point of failure?**
A: It runs at least 2 replicas behind a Kubernetes Service, is stateless, and the agent can scale or restart it.
