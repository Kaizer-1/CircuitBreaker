# 06 — Syllabus Mapping

Where every syllabus topic is covered: in the design docs now (Phase 0) and in the features built later.
Phase numbers refer to the [roadmap](07-roadmap.md). Textbook: *Microservices: Up and Running* (Mitra &
Nadareishvili).

## Unit I — Microservices foundations

| Topic | Where it is covered (docs) | Where it shows up in the build |
|---|---|---|
| Microservices basics | [00-overview](00-overview.md) (glossary), [ADR-0001](adr/0001-microservices-over-monolith.md), [system architecture](03-architecture/01-system-architecture.md) | 8 independently deployable services (Phases 1–7) |
| Reducing coordination costs | [service sizing](02-domain/03-service-sizing.md), [team topology](04-team-topology.md) ("How this reduces coordination"), [event catalog](03-architecture/03-event-catalog.md) (contracts instead of meetings) | CODEOWNERS, per-service CI path filters, versioned contracts (Phases 1, 6) |
| Team Topologies | [team topology](04-team-topology.md) — 4 team types, 3 interaction modes, owner study guides | Ownership labels on alerts and dashboards (Phase 5) |
| SEED(S): actors | [01-actors](01-seeds/01-actors.md) | — |
| SEED(S): jobs to be done | [02-jobs-to-be-done](01-seeds/02-jobs-to-be-done.md) | — |
| SEED(S): sequence diagrams | [03-sequence-diagrams](01-seeds/03-sequence-diagrams.md) (6 diagrams) | Integration tests follow these flows (Phases 2–7) |
| SEED(S): actions and queries | [04-actions-and-queries](01-seeds/04-actions-and-queries.md) (A-01…A-18, Q-01…Q-23) | — |
| SEED(S): OpenAPI spec | [`api/`](../api/README.md) — 7 OpenAPI 3.1 specs, lint-clean | Contract tests against the specs (Phase 1 onwards) |
| SEED(S): feedback | [`api/README.md` — Feedback](../api/README.md#feedback-seeds-step-6) | Mock server for web/bots before backend exists (Phase 1) |
| Microservices vs APIs | [system architecture §4](03-architecture/01-system-architecture.md#4-microservices-vs-apis) | Public API (gateway) stays stable while services change behind it |

## Unit II — Boundaries, DDD and data

| Topic | Where it is covered (docs) | Where it shows up in the build |
|---|---|---|
| Finding service boundaries | [event storming §4](02-domain/01-event-storming.md#step-4--how-the-boundaries-emerged), [bounded contexts](02-domain/02-bounded-contexts.md) | One service per context |
| Domain-Driven Design | [bounded contexts](02-domain/02-bounded-contexts.md) (ubiquitous language, subdomains, context map, ACL), aggregates in [event storming](02-domain/01-event-storming.md#step-3--aggregates) | `domain/` packages per service (Phase 2) |
| Event Storming | [01-event-storming](02-domain/01-event-storming.md) — timeline, commands, policies, hotspots, aggregates | — |
| Universal Sizing Formula | [service sizing](02-domain/03-service-sizing.md) (⚠️ verify wording) | Re-sizing triggers revisited in Phase 8 |
| Independent deployability | [ADR-0001](adr/0001-microservices-over-monolith.md), [sizing H6](02-domain/03-service-sizing.md), [deployment §4](03-architecture/07-deployment-and-environments.md#4-cicd-pipeline-github-actions) | Per-service pipelines; deploy one service without others (Phase 6) |
| Data ownership | [data ownership](03-architecture/02-data-ownership.md) | Keyspace/DB per service with per-service credentials (Phase 1) |
| Event Sourcing | [ES and CQRS](03-architecture/04-event-sourcing-and-cqrs.md), [ADR-0005](adr/0005-event-sourcing-for-matching-engine.md) | Matching engine event store, snapshots, replay (Phase 2) |
| CQRS | [ES and CQRS](03-architecture/04-event-sourcing-and-cqrs.md), [ADR-0006](adr/0006-cqrs-portfolio-read-model.md) | Portfolio and market-data read models (Phases 2–3); time travel (Phase 4) |

## Unit III — DevOps and infrastructure

| Topic | Where it is covered (docs) | Where it shows up in the build |
|---|---|---|
| DevOps principles | [deployment §4](03-architecture/07-deployment-and-environments.md#4-cicd-pipeline-github-actions) (build once, deploy many; automation; fast feedback), [coding standards §11](05-coding-standards.md#11-git-workflow-and-commit-messages) (trunk-based) | GitHub Actions pipeline (Phase 6); team runs what it builds |
| IaC environment and pipeline | [deployment §5](03-architecture/07-deployment-and-environments.md#5-infrastructure-as-code-terraform--localstack), [ADR-0010](adr/0010-terraform-localstack.md) | Terraform modules for staging/prod against LocalStack, plan in PR, apply in CI (Phase 6) |
| Infrastructure components | [system architecture](03-architecture/01-system-architecture.md) (container inventory), [ADR-0003](adr/0003-redpanda-event-bus.md), [ADR-0004](adr/0004-cassandra-event-store.md), [ADR-0008](adr/0008-observability-stack.md) | Redpanda, Cassandra, Postgres, Prometheus, Grafana, Loki (Phases 1, 5) |

## Unit IV — Building microservices

| Topic | Where it is covered (docs) | Where it shows up in the build |
|---|---|---|
| Coding standards and developer setup | [coding standards](05-coding-standards.md) | `make setup`, ruff/mypy/pytest, pre-commit (Phase 1) |
| Docker | [coding standards §1, §3](05-coding-standards.md), [deployment §2](03-architecture/07-deployment-and-environments.md#2-docker-compose-dev) | Dockerfile per service, Docker Compose (Phase 1) |
| Cassandra | [ADR-0004](adr/0004-cassandra-event-store.md), [data ownership §4](03-architecture/02-data-ownership.md#4-table-designs) | Event store and query tables (Phase 2) |
| Kubernetes | [ADR-0007](adr/0007-kubernetes-kind-and-helm-umbrella.md), [deployment §3](03-architecture/07-deployment-and-environments.md#3-kubernetes-with-kind--helm-umbrella-chart) | kind cluster, probes, rollouts, RBAC (Phase 5) |
| Endpoint design | [`api/`](../api/README.md), [coding standards §4, §9](05-coding-standards.md#9-api-versioning-rules) | FastAPI routers matching the specs (Phases 2–7) |
| Umbrella project | [deployment §3](03-architecture/07-deployment-and-environments.md#3-kubernetes-with-kind--helm-umbrella-chart) (Helm umbrella chart), [coding standards §2](05-coding-standards.md#2-repository-layout-monorepo) (monorepo; ⚠️ verify textbook meaning) | `charts/circuitbreaker` + root Makefile and Compose (Phases 1, 5) |

## Unit V — Releasing and evolving

| Topic | Where it is covered (docs) | Where it shows up in the build |
|---|---|---|
| Staging environment | [deployment §1](03-architecture/07-deployment-and-environments.md#1-the-path-from-laptop-to-production) | `staging` namespace, automatic deploy on merge, chaos rehearsal gate (Phase 6) |
| Shipping containers | [deployment §4](03-architecture/07-deployment-and-environments.md#4-cicd-pipeline-github-actions) | Images tagged by git SHA in GHCR, same image promoted to prod (Phase 6) |
| Managing change | [deployment §6](03-architecture/07-deployment-and-environments.md#6-managing-change), [API versioning](05-coding-standards.md#9-api-versioning-rules), [event versioning](03-architecture/03-event-catalog.md#versioning-rules), [ADR process](adr/README.md) | Expand-and-contract migrations, `/v2` side-by-side, auto-rollback (Phases 6–8) |
| Measuring microservices transformation progress | [roadmap — Measuring progress](07-roadmap.md#measuring-progress) | DORA + architecture metrics dashboard (Phase 8) |

## Beyond the syllabus (clearly marked as extras)

| Extra | Where |
|---|---|
| AI SRE agent with tool allowlist (via OpenRouter) | [SRE agent design](03-architecture/05-sre-agent-design.md), [ADR-0009](adr/0009-sre-agent-fixed-tool-allowlist.md), [ADR-0013](adr/0013-openrouter-llm-gateway.md) |
| Chaos engineering | [chaos scenarios](03-architecture/06-chaos-scenarios.md) |
| Circuit breaker pattern | [system architecture §3](03-architecture/01-system-architecture.md#3-synchronous-vs-asynchronous-communication) |

## Viva Notes

**Q: Which syllabus topic is best demonstrated live?**
A: Event sourcing and CQRS through time travel, and independent deployability through a live bad deploy that the agent
rolls back without touching any other service.

**Q: Where is the SEED(S) "feedback" step in your project?**
A: Consumers review spec PRs, the UI is built against a mock server generated from the specs before the backend exists,
and contract tests keep code and specs in sync. Phase 0 feedback already changed the design (async cancel, orphan rule).

**Q: How do you measure whether moving to microservices worked?**
A: DORA metrics (deployment frequency, lead time, change failure rate, time to restore) plus architecture metrics such as
the share of changes that touch only one service. See the roadmap.
