# api-gateway

> **Status:** Phase 3 — sessions (JWT), role checks, identity headers, rate limits, HTTP proxying and the WebSocket
> proxy are built. Layout:
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
| `api/ws_proxy.py` | The WebSocket channels Q-06, Q-10, Q-13, Q-19: verify `?access_token=`, check the role, open the same channel upstream with identity headers from the token, relay both ways |
| `log_redaction.py` | Rewrites `access_token=…` to `access_token=[redacted]` in every log line (uvicorn logs each WebSocket URL) |

**Roles per route** (from [`api/api-gateway.yaml`](../../api/api-gateway.yaml)): orders and portfolio: `trader`, `bot`;
market data, time travel, leaderboard, incident list and detail: any signed-in role; agent settings, decisions and
chaos: `admin`. A missing, expired or forged token is `401 UNAUTHENTICATED`; the wrong role is `403 FORBIDDEN`. The
admin has no trading account, so no `X-Trader-Id` is ever sent for an admin.

**Rate limits:** 20 requests/s per trader, 50/s per bot token, 20/s per client address on the session endpoints (no
token yet); then `429 RATE_LIMITED` with `Retry-After`. A token bucket in memory, **per replica**: with 2 replicas a
caller can reach twice the limit, which is enough to stop a runaway client.

**`GET /v1/system/status`** (the System Status page and `make status`) is **admin-only** since Phase 2.

**WebSockets** ([api/README.md](../../api/README.md#websocket-channels)). Browsers cannot set headers on a WebSocket,
so the token comes as `?access_token=`. Q-06 market, Q-13 replay and Q-19 incidents: any signed-in role; Q-10 portfolio:
`trader` or `bot` (an admin gets `4403`). A missing, expired or forged token closes with `4401`; an unreachable upstream
with `1011`. The upstream gets `X-Trader-Id` / `X-Role` / `X-Nickname` built from the token (a client's own copies are
never forwarded, so a trader can only open their own portfolio stream) and every query parameter except the token. From
then on the gateway is a pipe: messages go both ways unchanged and the upstream's close code reaches the client when it
is `1000`, `1013` (a full replay replica: try again later) or any `4xxx` (e.g. `4404`); any other becomes `1011`. The token is never logged: the access log has the path only, and uvicorn's own line for each WebSocket is
redacted. Connections are not rate-limited (they are long-lived; the services' heartbeat closes dead ones).

### Nickname blocklist (projector safety)

Nicknames appear on the projector and the leaderboard, so A-01 refuses a few offensive words in English and
Hindi/Hinglish before any account is opened: `422 VALIDATION_FAILED` with the polite detail "That nickname can't be
shown on the big screen. Please choose another one." (the phone shows it as is). The words are data, not code:
[`src/api_gateway/domain/nickname_blocklist.txt`](src/api_gateway/domain/nickname_blocklist.txt), one per line, in a
`[blocked]` and an `[allowed]` section; `domain/nicknames.py` applies them. A refused name is not logged.

How a nickname is checked: lowercase; leetspeak mapped (0→o, 1→i, 3→e, 4→a, @→a, $→s, 5→s, 7→t); underscores and
digits removed; repeated letters collapsed (`fuuuck` → `fuck`); then a blocked word anywhere inside refuses it, unless
that occurrence lies entirely inside an allowed word. List entries are normalised the same way.

**Why the `[allowed]` section.** Substring matching on its own would refuse ordinary names that contain a short
blocked word, and the audience is mostly Indian: Harshit, Ishita, Akshita, Kshitij and Dikshit contain "shit"; Nigam
and Nigar contain "nigga" once its double g collapses; Nazim and Nazia contain "nazi"; Randip contains "randi". Those
words and name fragments are listed as allowed, and `tests/unit/domain/test_nicknames.py` keeps a list of 47 ordinary
names that must always pass next to the obvious, leetspeak and padded variants that must be refused. Blocked entries
are at least four letters long, and very short or very common words (*ass*, *sala*, *kutta*) are left out on purpose.

**Limits, accepted.** A determined person can always find a spelling no list catches; this stops the obvious and the
lazily disguised. An allowed word can shelter a blocked one inside it (a slur plus one letter can spell an allowed
name), and a blocked word can also be a real place name ("Niger" is refused). To change the list, edit the file and add
the case to the tests.

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

**Business metrics:** `gateway_ws_connections{channel}` (WebSocket connections proxied right now).

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
