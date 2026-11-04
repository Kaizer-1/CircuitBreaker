# api-gateway

> **Status:** Phase 2 — sessions (JWT), role checks, identity headers, rate limits and proxying are built; the
> WebSocket proxy arrives in Phase 3. Layout:
> [coding standards §3](../../docs/05-coding-standards.md#3-folder-layout-inside-every-service) (package `api_gateway`).

| | |
|---|---|
| **Owning team** | API Team (the textbook's consumer-facing API team: owns the APIs at the system boundary) |
| **Owner** | **MD Asif Ali** |
| **Team type** | Stream-aligned ([team topology](../../docs/04-team-topology.md)) |
| **Bounded context** | Access (generic subdomain) |

## Responsibility

The single public entry point. Issues and verifies JWT sessions (trader, bot, admin), checks roles, applies rate limits (20 req/s per trader, 50 req/s per bot token), adds `X-Correlation-Id`, strips client-supplied identity headers and sets `X-Trader-Id` / `X-Role` / `X-Nickname`, proxies HTTP and WebSocket traffic to the owning service, and refuses every `/internal/` path.

## How it works

| Code | Role |
|---|---|
| `domain/sessions.py` | Issue and verify HS256 JWTs: `sub` (trader_id, or the nickname for an admin), `role`, `nickname`, `iat`, `exp` (12 h) |
| `domain/rate_limit.py` | A token bucket per caller |
| `api/sessions.py` | A-01 join (calls portfolio A-02, kind `HUMAN`), A-03 bot session (`X-Bot-Secret`; kind `BOT` or `LOAD`), A-04 admin (passcode), Q-01 |
| `api/auth.py` | Per route: verify the bearer token, check the role, apply the rate limit |
| `api/proxied.py` | Every other public route, proxied with `X-Trader-Id` / `X-Role` / `X-Nickname` from the token |

**Roles per route** (from [`api/api-gateway.yaml`](../../api/api-gateway.yaml)): orders and portfolio: `trader`, `bot`;
market data, time travel, leaderboard, incident list and detail: any signed-in role; agent settings, decisions and
chaos: `admin`. A missing, expired or forged token is `401 UNAUTHENTICATED`; the wrong role is `403 FORBIDDEN`. The
admin has no trading account, so no `X-Trader-Id` is ever sent for an admin.

**Rate limits:** 20 requests/s per trader, 50/s per bot token, 20/s per client address on the session endpoints (no
token yet); then `429 RATE_LIMITED` with `Retry-After`. A token bucket in memory, **per replica**: with 2 replicas a
caller can reach twice the limit, which is enough to stop a runaway client.

**`GET /v1/system/status`** (the System Status page and `make status`) is **admin-only** since Phase 2.

## Owned data

None (stateless). Sessions are self-contained signed JWTs (`JWT_SECRET`). Secrets `JWT_SECRET`, `BOT_SECRET` and
`ADMIN_PASSCODE` have no defaults; Compose sets local-development placeholders.

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

- none of its own: it needs no database or broker. `/readyz` is `200` once it is configured and running. `GET /v1/system/status` (admin token) asks every other service's `/readyz`.

The gateway reads `SERVICE_UPSTREAMS` (a JSON map of service name to base URL; the defaults are the host ports of `make up`).

```bash
curl -s -X POST localhost:8080/v1/sessions -H 'Content-Type: application/json' -d '{"nickname":"RajuTrader"}'
curl -s -H "Authorization: Bearer <access_token>" localhost:8080/v1/portfolio
```

## Viva Notes

**Q: Why put authentication in the gateway?**
A: One place to verify tokens and roles, so no service re-implements security; services trust identity headers only from the gateway (network policies).

**Q: Why does the gateway strip `X-Trader-Id` from clients?**
A: Otherwise a client could pretend to be someone else. Only the gateway, after verifying the JWT, may set it.

**Q: Is the gateway a single point of failure?**
A: It runs at least 2 replicas behind a Kubernetes Service, is stateless, and the agent can scale or restart it.
