# 05 — Coding Standards and Developer Setup

> These rules exist so that any team member can open any service and feel at home, and so that CI can enforce
> quality automatically instead of by argument. Consistency lowers **cognitive load** and **coordination cost**.

## 1. Languages and tools

| Area | Choice | Notes |
|---|---|---|
| Backend language | **Python 3.12** | All 8 services ([ADR-0002](adr/0002-python-fastapi.md)) |
| Web framework | **FastAPI** + **Pydantic v2** | Request/response models generate OpenAPI we compare against `api/` |
| Dependency management | **uv** with `pyproject.toml` + lock file per service | Fast, reproducible installs; each service pins its own dependencies |
| Lint + format (Python) | **ruff** (`ruff check`, `ruff format`) | One fast tool for linting, import sorting and formatting |
| Type checking | **mypy** (strict on `domain/` and `application/`) | Catches bugs in the business logic without slowing adapters |
| Tests | **pytest**, **pytest-asyncio**, **testcontainers** (integration) | |
| Frontend | React + Vite + TypeScript + TradingView Lightweight Charts; **eslint** + **prettier** | Versions pinned in Phase 1 |
| API lint | `npx @redocly/cli lint` | Must pass with 0 errors |
| Containers | Docker, multi-stage builds, non-root user | |
| Kubernetes | kind, kubectl, Helm 3 | |
| IaC and deployment | Terraform ≥ 1.6 (kind, kubernetes, helm providers), Argo CD; LocalStack optional | |

## 2. Repository layout (monorepo)

```text
CircuitBreaker/
├── README.md, CLAUDE.md (local only), .editorconfig, .gitignore, redocly.yaml, Makefile, docker-compose.yml
├── docs/                    # design docs (source of truth) + ADRs
├── api/                     # OpenAPI specs + WebSocket/event docs
├── contracts/events/        # JSON Schemas of integration events (from Phase 2, with the first producers)
├── services/<service>/      # one folder per service (layout below)
├── web/                     # React frontend
├── dev/                     # local-development support: Redpanda/Cassandra/Postgres init scripts for Compose
├── scripts/                 # make doctor, wait-for-stack, git hooks
├── tools/                   # stub generator, service scaffold, template-drift check
├── charts/circuitbreaker/   # Helm umbrella chart
├── deploy/                  # GitOps config Argo CD watches: envs/<env>/<service>.yaml (image tags), argocd/
├── infra/                   # Terraform: modules/ (kind-cluster, environment, argo-cd, optional localstack) + envs/local
├── observability/           # Prometheus rules, Grafana dashboards (as code)
└── .github/workflows/       # CI/CD
```

**Why a monorepo?** Three people, one course: one place to clone, one PR can update a spec and its docs together,
and CI path filters still build and deploy each service independently. Each service folder is self-contained
(own dependencies, Dockerfile, tests) so it could be moved to its own repository later without code changes.

### The umbrella project (our interpretation)

> **Umbrella project:** the top-level project that pulls every service together so the whole system can be run as
> one. *This definition is **our interpretation** of the syllabus term; it is not taken from the textbook chapters we
> have read.*

We use the idea at two levels:

| Level | Our umbrella | What it pulls together |
|---|---|---|
| **Local** | The repository root: `docker-compose.yml` + `Makefile` | Every service (built from `services/<name>/`), the data stores and observability, started with `make up`; the init jobs create the topics and keyspaces ([developer setup](08-developer-setup.md)). |
| **Kubernetes** | The Helm umbrella chart `charts/circuitbreaker` | One sub-chart per service plus the data stores, with shared defaults. Argo CD deploys each service from it ([deployment §3](03-architecture/07-deployment-and-environments.md#3-kubernetes-with-kind--helm-umbrella-chart)). |

A common alternative is a separate "workspace" repository that pulls in one repository per service (for example with
git submodules). With a monorepo, the root itself plays that role.

## 3. Folder layout inside every service

Every Python service follows the same **ports-and-adapters** (hexagonal) layout: business rules in the middle with no
I/O, and everything that talks to the outside world at the edges.

```text
services/order-service/
├── README.md                 # responsibility, data, events, owner (kept up to date)
├── Dockerfile                # multi-stage, non-root, exposes 8000
├── .env.example              # every environment variable, with local-development values
├── pyproject.toml            # dependencies + ruff/mypy/pytest config
├── uv.lock
├── src/order_service/
│   ├── main.py               # create_app(): FastAPI app, routers, lifespan (start consumers)
│   ├── config.py             # Settings (pydantic-settings) read from env vars
│   ├── business_metrics.py   # the service's own Prometheus metrics (the ones named in the docs)
│   ├── runtime/              # IDENTICAL in every service: logging, Problem Details, middleware, metrics, health, events, Kafka
│   ├── api/                  # HTTP routers: thin — parse, call a use case, map errors to Problem Details
│   ├── application/          # use cases (place_order, cancel_order, apply_matching_event)
│   ├── domain/               # pure business logic and models (Order, OrderStatus, validation rules)
│   ├── ports/                # interfaces the application needs (OrderRepository, EventPublisher, PortfolioClient)
│   ├── adapters/             # implementations: cassandra/, redpanda/, http/
│   ├── events/               # models of produced/consumed events (match contracts/events/)
│   └── migrations/           # CQL (Cassandra) or SQL (Postgres) migrations, numbered; applied at start-up
└── tests/
    ├── unit/                 # domain + application, no I/O, fast
    ├── integration/          # real Cassandra/Redpanda/Postgres via testcontainers
    └── contract/             # API responses vs api/<service>.yaml; events vs contracts/events/
```

**Rules:** `domain/` imports nothing from `api/` or `adapters/`. `api/` never talks to a database directly. Adapters
are chosen in `main.py` (dependency injection), so tests can swap them for fakes.

### Duplication over coupling: why the services share no library

Every service needs the same plumbing: JSON logging with a correlation ID, Problem Details errors, request metrics,
health and readiness endpoints. The obvious move is a shared Python package. We deliberately do **not** do that.

| | Shared library | Copy + drift check (our choice) |
|---|---|---|
| A fix or feature | Release the library, then every team upgrades | Change the template service and copy it; CI shows any service that differs |
| Deploying one service | May need a new library version first, so releases get linked | Independent: a service contains everything it runs |
| Who must agree on a change | Every team that uses it | Only the service that changes |
| Cost | Coupling and lock-step upgrades: the coordination cost we are trying to remove | Some duplicated code, and a script that keeps the copies equal |

**How it works.** The plumbing lives in `src/<package>/runtime/` (plus the `Dockerfile` and a few shared tests). The first
service, `order-service`, is the template. `tools/scaffold.py` copies those files into a new service and renames the package;
`tools/check_template_drift.py` (part of `make lint` and CI) compares every service's copy, after renaming, and fails if
any differs. To change the plumbing: edit it in `order-service`, run `python3 tools/check_template_drift.py --sync` to copy
it to the others, and the check goes green.

This follows the textbook's reason for microservices (minimise coordination between teams) and its warning that a
shared kernel needs tight coordination ([bounded contexts](02-domain/02-bounded-contexts.md#context-map)). Real business
rules are never copied: they live in each service's `domain/` and are owned by exactly one team.

## 4. Naming conventions

| Thing | Convention | Example |
|---|---|---|
| Service name (folder, Docker image, Helm release, K8s Deployment, `service` log/metric label) | `kebab-case` | `market-data-service` |
| Python package | `snake_case` of the service name | `market_data_service` |
| Python modules, functions, variables | `snake_case` | `apply_trade_executed()` |
| Python classes, Pydantic models | `PascalCase` | `OrderBook`, `PlaceOrderRequest` |
| Constants | `UPPER_SNAKE_CASE` | `TICK_SIZE_PAISE = 5` |
| HTTP paths | lower-case, plural nouns, kebab-case words, versioned | `/v1/orders/{order_id}`, `/v1/market/{ticker}/order-book` |
| Path/query parameters, JSON fields | `snake_case` | `order_id`, `limit_price_paise` |
| Money fields | integer, suffix `_paise` | `cash_paise` |
| Time fields | ISO-8601 UTC string, suffix `_at` | `placed_at` |
| Enum values | `UPPER_SNAKE_CASE` | `PARTIALLY_FILLED` |
| Event types | `PascalCase`, past tense | `TradeExecuted` |
| Topics | `<stream>.v<major>` | `matching.v1` |
| Consumer groups | `<service>.<topic>` | `portfolio-service.matching.v1` |
| Cassandra keyspaces | `snake_case` of the service | `order_service` |
| Cassandra tables | `<things>_by_<key>` when it is a query table | `orders_by_trader` |
| Environment variables | `UPPER_SNAKE_CASE`, service-agnostic where shared | `KAFKA_BOOTSTRAP_SERVERS`, `CASSANDRA_KEYSPACE` |
| Prometheus metrics | `snake_case`, unit suffix, `_total` for counters | `orders_placed_total`, `http_request_duration_seconds` |
| Action/query IDs | `A-xx`, `Q-xx` | `A-05` |

## 5. Formatting and linting

- `ruff format` (line length 100) and `ruff check` with rule sets `E, F, I, B, UP, SIM, N, ASYNC, S`.
- `.editorconfig` at the root sets UTF-8, LF, 4-space Python, 2-space YAML/JSON/Markdown.
- CI fails on any lint error. No `# noqa` without a comment explaining why.
- `mypy --strict` must pass for `domain/` and `application/`.

## 6. Testing rules

| Level | What | Rule |
|---|---|---|
| **Unit** | `domain/` and `application/` with fakes for ports | Fast (< 1 s per test file). Required for every business rule. |
| **Matching engine** | Price-time priority, partial fills, market orders, protection price, cancel race, snapshot/replay equivalence | **100% branch coverage** of `domain/`; property-based tests (Hypothesis): replaying stored events always rebuilds the same book. |
| **Integration** | Adapters against real Cassandra/Postgres/Redpanda (testcontainers) | Run in CI on every PR touching the service. |
| **Contract (API)** | Responses validated against `api/<service>.yaml` | A response that doesn't match the spec fails the build. |
| **Contract (events)** | Produced events validated against `contracts/events/*.schema.json`; consumers tested with the catalog's examples | Protects other teams from accidental breaking changes. |
| **Smoke** | After every deploy to staging: join, place order, see fill, time-travel query | Run by CI. |
| **Chaos rehearsal** | `BAD_DEPLOY` in staging must be remediated within 3 minutes | Run by CI before prod promotion. |

Coverage target: **≥ 80% lines** per service (100% branches for matching `domain/`). Test names describe behaviour:
`test_market_buy_stops_at_protection_price`.

## 7. Standard operational endpoints

Every service exposes these on port 8000 (not in the OpenAPI specs, not routed by the gateway). The readiness body is
`{"status": "ready" | "not_ready", "service": ..., "checks": {"redpanda": {"ok": ..., "latency_ms": ...}, ...}}`.

| Endpoint | Purpose | Contract |
|---|---|---|
| `GET /healthz` | **Liveness**: process is alive | `200 {"status":"ok"}`; never checks dependencies (a DB outage must not cause restart loops) |
| `GET /readyz` | **Readiness**: ready for traffic | `200` only if its own store and Redpanda are reachable and consumers are caught up; else `503` with details |
| `GET /metrics` | Prometheus metrics | Standard metrics below + service-specific ones |

The **api-gateway** has one more operational endpoint, `GET /v1/system/status`. It calls every service's `/readyz`
and returns one combined report (`status`: `healthy` | `unhealthy`; `services`; `dependencies` grouped from the services'
checks). The System Status page in `web` shows it. Like `/healthz` it is not derived from a job to be done, so it has no
A/Q ID and is not in `api/api-gateway.yaml`. It is **admin-only** (since Phase 2): the System Status page asks for the
presenter passcode (A-04), and `make status` signs in with the local placeholder passcode first.

**Standard metrics** (all services, labelled `service`, plus `route`, `method`, `status` where relevant):
`http_requests_total`, `http_request_duration_seconds` (histogram), `http_requests_in_flight`,
`events_published_total{topic,event_type}`, `events_consumed_total{topic,event_type}`,
`event_processing_errors_total`, `events_dead_lettered_total{topic}`, `consumer_lag_messages{topic,partition}`, plus Python process metrics
(`process_resident_memory_bytes`, CPU). These names are what the alert rules and the SRE agent's evidence collector
query, so they must not change without updating [06-chaos-scenarios.md](03-architecture/06-chaos-scenarios.md).

## 8. Logging

- **JSON to stdout**, one line per event; no log files inside containers.
- Required fields: `ts`, `level`, `service`, `version`, `message`, `correlation_id`; plus `trader_id`, `order_id`,
  `incident_id` when relevant.
- Levels: `debug` (off in prod), `info` (business milestones), `warning` (recovered problems), `error` (needs
  attention — the SRE agent reads these).
- Never log secrets, JWTs, passcodes or full request bodies.

## 9. API versioning rules

1. Every path starts with a major version: `/v1/…`. Internal paths: `/internal/v1/…`.
2. **Non-breaking** changes stay in `v1`: new endpoints, new optional request fields, new response fields, new enum
   values *only* where the spec says clients must tolerate unknown values.
3. **Breaking** changes (remove/rename a field, change a type or meaning, make an optional field required, change
   status codes) require `/v2/…`. Serve `v1` and `v2` side by side until all clients (web, bots) have moved, then remove
   `v1` with an announcement in the changelog.
4. The OpenAPI spec is updated **in the same PR** as the code; contract tests enforce they match.
5. `info.version` in each spec follows semantic versioning (`MAJOR.MINOR.PATCH`); MAJOR equals the path version.
6. Events follow the equivalent rules ([event catalog](03-architecture/03-event-catalog.md#versioning-rules)).

## 10. Configuration and secrets

- Twelve-factor style: all configuration from **environment variables**, parsed by one `Settings` class per service.
- Secrets (`JWT_SECRET`, `BOT_SECRET`, `ADMIN_PASSCODE`, `OPENROUTER_API_KEY`, DB passwords) come from Kubernetes
  Secrets created by Terraform from git-ignored inputs; in dev from a local `.env` (git-ignored, with a committed
  `.env.example`). Terraform state (`*.tfstate`) also contains them and is git-ignored.
- Feature flags for chaos hooks: `CHAOS_HOOKS_ENABLED` (default `false`).

## 11. Git workflow and commit messages

- **Trunk-based development:** short-lived branches (≤ 2 days) off `main`, merged by pull request.
- **Code owners:** `.github/CODEOWNERS` maps each `services/<name>/` folder to its owner; the owner must approve.
- **Commit messages: Conventional Commits** — `<type>(<scope>): <summary>` in the imperative mood, ≤ 72 characters.

| Type | Use for |
|---|---|
| `feat` | New behaviour |
| `fix` | Bug fix |
| `docs` | Documentation only |
| `test` | Tests only |
| `refactor` | Code change with no behaviour change |
| `chore` | Tooling, dependencies |
| `ci` | Pipeline changes |
| `infra` | Terraform, Helm, Kubernetes |

Scope is the service name, `api`, `web`, `charts`, `infra` or `docs`. Examples:
`feat(matching-engine): support market order protection price` · `fix(portfolio-service): release leftover cash on fill`
· `docs(adr): add ADR-0011 sync reservation`. Breaking changes add `!` (`feat(order-service)!: …`) and a
`BREAKING CHANGE:` footer.

## 12. Documentation rule

**Docs are the source of truth.** A change that alters behaviour, a contract, a name or a decision must update the
relevant docs, specs and ADRs **in the same pull request**. Decisions that reverse an ADR get a new ADR that
supersedes it (never silently edit history).

## 13. Developer setup

The full guide (prerequisites, first run, ports, measured resource use, adding a service, troubleshooting) is
[08-developer-setup.md](08-developer-setup.md). In short, you need Docker Desktop (**10 GB** recommended; Phase 1
measured about 1.8 GiB in use, see the guide), Python 3.12 + uv, Node.js 24 LTS (≥ 22.12), kind, kubectl, Helm 3,
gitleaks and pre-commit. Terraform, the Argo CD CLI and an OpenRouter API key come later (Phases 5–7); LocalStack only
if you enable the optional module. `make doctor` checks all of this.

| Command | What it does |
|---|---|
| `make doctor` | Check the prerequisites and print their versions |
| `make setup` | Install Python/Node dependencies and the git hooks (ruff, gitleaks, redocly, prettier, block-private) |
| `make infra` | Start only the infrastructure in Docker Compose: Redpanda, Cassandra, Postgres and their init jobs |
| `make up` / `make down` / `make reset` | Start everything / stop it (data kept) / stop it and wipe the data volumes |
| `make demo-reset` | Wipe the data, start everything and check the clean starting state (before a rehearsal) |
| `make logs s=<service>`, `make ps`, `make status` | Follow logs / container status / combined system status |
| `make test s=order-service` | Unit, smoke and contract tests for one service (all if `s` is omitted) |
| `make lint` / `make fmt` | ruff, mypy, redocly, template drift, eslint and prettier / auto-format |
| `make k8s-up` / `make k8s-down` / `make test-k8s` | The system on a local kind cluster with Prometheus, Grafana and Loki / stop it (data kept) / its smoke test ([developer setup §11](08-developer-setup.md#11-kubernetes-kind)) |
| `make platform` | *(Phase 6)* Terraform plan + apply of `infra/envs/local`: kind cluster, namespaces, RBAC, Secrets, Argo CD |
| `make sync env=staging` | Ask Argo CD to refresh and sync now (instead of waiting for its poll) |
| `make promote s=<service>` | Open a PR copying the service's staging image tag to `deploy/envs/prod/` |

## Viva Notes

**Q: Why the ports-and-adapters layout?**
A: Business rules (`domain/`) don't depend on databases or frameworks, so they are easy to test with fakes and the
storage or broker could be swapped by changing one adapter.

**Q: How do you stop the code and the API spec drifting apart?**
A: Contract tests validate real responses against `api/<service>.yaml`, the spec is linted in CI, and the rule is that
specs change in the same PR as the code.

**Q: What makes an API change "breaking"?**
A: Anything an existing client could trip over: removing or renaming a field, changing a type or meaning, making
something required, or changing status codes. Those need `/v2/`; adding optional things doesn't.

**Q: Why do the services copy the same runtime code instead of importing a shared library?**
A: Duplication over coupling. A shared library is one more thing every service must upgrade together, so a change to it
becomes a lock-step release across teams, which is exactly the coordination cost microservices exist to avoid. So each
service owns its copy, and a CI check (`tools/check_template_drift.py`) fails if the copies drift apart. The price is
that a fix is applied in 8 places, which the scaffold and the drift check make cheap and visible.

**Q: Why does `/healthz` not check the database?**
A: Liveness failure makes Kubernetes restart the pod. If the database is down, restarting every service won't help
and causes restart storms. Dependency checks belong in `/readyz`, which only removes the pod from traffic.
