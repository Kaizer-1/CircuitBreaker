# 06 — Syllabus Mapping

Where every syllabus topic is covered: in the design docs now (Phase 0) and in the features built later. Phase numbers
refer to the [roadmap](07-roadmap.md). Textbook: *Microservices: Up and Running* (Mitra & Nadareishvili).

**Basis column:**

- **Ch. N:** aligned with that textbook chapter. We have read Chapters 2, 4 and 7.
- **Syllabus:** follows the course syllabus headings.
- **Standard:** a named industry source, cited in the linked doc.
- **Project:** our own design.

## Unit I — Microservices foundations

| Topic | Where it is covered (docs) | Where it shows up in the build | Basis |
|---|---|---|---|
| Microservices basics and goal | [00-overview](00-overview.md) glossary (definition + goal: minimise coordination costs, speed and safety at scale), [ADR-0001](adr/0001-microservices-over-monolith.md), [system architecture](03-architecture/01-system-architecture.md) | 8 independently deployable services (Phases 1–7) | Ch. 4 (goal); Standard — Nadareishvili et al. 2016 (definition) |
| Reducing coordination costs | [service sizing](02-domain/03-service-sizing.md), [team topology](04-team-topology.md#how-this-reduces-coordination), [event catalog](03-architecture/03-event-catalog.md) (contracts instead of meetings) | CODEOWNERS, per-service CI path filters, one Argo CD Application per service, versioned contracts (Phases 1, 6) | Ch. 2, Ch. 4 |
| Operating model and Team Topologies | [team topology](04-team-topology.md) — the textbook's 5 design steps; System Design, microservices team template (Trading, Market Insights), Cloud Platform, Release & Reliability, API Team; 4 team types, 3 interaction modes, team-size principle | Ownership labels on alerts and dashboards (Phase 5); CODEOWNERS on `deploy/envs/prod/` for the release team (Phase 6) | Ch. 2 |
| SEED(S): actors | [01-actors](01-seeds/01-actors.md) | — | Syllabus |
| SEED(S): jobs to be done | [02-jobs-to-be-done](01-seeds/02-jobs-to-be-done.md) | — | Syllabus |
| SEED(S): sequence diagrams | [03-sequence-diagrams](01-seeds/03-sequence-diagrams.md) (6 diagrams) | Integration tests follow these flows (Phases 2–7) | Syllabus |
| SEED(S): actions and queries | [04-actions-and-queries](01-seeds/04-actions-and-queries.md) (A-01…A-18, Q-01…Q-23) | — | Syllabus |
| SEED(S): open-standard spec | [`api/`](../api/README.md) — 7 OpenAPI 3.1 specs, lint-clean; events as JSON Schemas in the [event catalog](03-architecture/03-event-catalog.md) | Contract tests against the specs (Phase 1 onwards) | Syllabus |
| SEED(S): feedback | [`api/README.md` — Feedback](../api/README.md#feedback-seeds-step-6) | Mock server for web/bots before the backend exists (Phase 1) | Syllabus |
| Microservices vs APIs | [system architecture §4](03-architecture/01-system-architecture.md#4-microservices-vs-apis) — the API at the system boundary (owned by the API Team) vs the microservices behind it | Public API (gateway) stays stable while services change behind it | Ch. 2 |

## Unit II — Boundaries, DDD and data

| Topic | Where it is covered (docs) | Where it shows up in the build | Basis |
|---|---|---|---|
| Finding service boundaries | [event storming — how the boundaries emerged](02-domain/01-event-storming.md#how-the-boundaries-emerged), [bounded contexts](02-domain/02-bounded-contexts.md) (edges where terms change meaning) | One service per context to start | Ch. 4 |
| Domain-Driven Design | [bounded contexts](02-domain/02-bounded-contexts.md) — ubiquitous language, subdomains, context map with shared kernel, upstream–downstream, customer–supplier, conformist, ACL, open host service, separate ways; sync vs async integration; [aggregates](02-domain/01-event-storming.md#aggregates) | `domain/` packages per service (Phase 2) | Ch. 4 (Published Language: Evans' DDD) |
| Event Storming | [01-event-storming](02-domain/01-event-storming.md) — the textbook's 5 phases: events, timeline + hotspots, reverse narrative/commands, aggregates/contexts, competitive analysis (S/M/L matrix) | — | Ch. 4 |
| Universal Sizing Formula | [service sizing §1](02-domain/03-service-sizing.md#1-the-universal-sizing-formula) — the three principles; boundaries ≠ bounded contexts; splitting is easier than merging; why we start at 8 | Re-sizing triggers revisited in Phase 8 | Ch. 4 |
| Independent deployability | [ADR-0001](adr/0001-microservices-over-monolith.md), [sizing H6](02-domain/03-service-sizing.md), [deployment §4](03-architecture/07-deployment-and-environments.md#4-cicd-pipeline-github-actions--argo-cd-gitops) | Per-service pipelines and Argo CD Applications; deploy one service without others (Phase 6) | Project |
| Data ownership and sharing data | [data ownership](03-architecture/02-data-ownership.md) — one owner per piece of data; [§6](03-architecture/02-data-ownership.md#6-how-services-share-data-without-a-shared-database) sync API calls, events + local copies, ES + CQRS read models | Keyspace/DB per service with per-service credentials (Phase 1) | Standard — Richardson, *Microservices Patterns* (2018) |
| Event Sourcing | [ES and CQRS](03-architecture/04-event-sourcing-and-cqrs.md), [ADR-0005](adr/0005-event-sourcing-for-matching-engine.md) | Matching engine event store, snapshots, replay (Phase 2) | Standard |
| CQRS | [ES and CQRS](03-architecture/04-event-sourcing-and-cqrs.md), [ADR-0006](adr/0006-cqrs-portfolio-read-model.md) | Portfolio and market-data read models (Phases 2–3); time travel (Phase 4) | Standard |

## Unit III — DevOps and infrastructure

| Topic | Where it is covered (docs) | Where it shows up in the build | Basis |
|---|---|---|---|
| DevOps principles | [deployment §4](03-architecture/07-deployment-and-environments.md#4-cicd-pipeline-github-actions--argo-cd-gitops) (build once, deploy many; everything in Git; pull, don't push; fast feedback), [coding standards §11](05-coding-standards.md#11-git-workflow-and-commit-messages) (trunk-based) | GitHub Actions CI + Argo CD (Phase 6); team runs what it builds | Ch. 7 (CD vs deployment); Project |
| IaC environment and pipeline | [deployment §5](03-architecture/07-deployment-and-environments.md#5-infrastructure-as-code-terraform), [ADR-0015](adr/0015-terraform-local-platform.md) (amends [ADR-0010](adr/0010-terraform-localstack.md)) — Terraform modules `kind-cluster`, `environment`, `argo-cd`, optional `localstack`, called from `envs/local` | `make platform`; CI validate + create–check–destroy of the platform (Phases 5–6) | Ch. 7 |
| Infrastructure components | [deployment §5 — mapping to the textbook](03-architecture/07-deployment-and-environments.md#how-this-maps-onto-the-textbooks-aws-infrastructure) (network → Kubernetes → GitOps server), [system architecture](03-architecture/01-system-architecture.md) (container inventory), [ADR-0003](adr/0003-redpanda-event-bus.md), [ADR-0004](adr/0004-cassandra-event-store.md), [ADR-0008](adr/0008-observability-stack.md) | kind, Argo CD, Redpanda, Cassandra, Postgres, Prometheus, Grafana, Loki (Phases 1, 5, 6) | Ch. 7 |
| GitOps deployment server | [ADR-0014](adr/0014-gitops-with-argo-cd.md), [deployment §4](03-architecture/07-deployment-and-environments.md#4-cicd-pipeline-github-actions--argo-cd-gitops) — Argo CD, automated sync, self-heal off, one Application per service | Argo CD syncs staging on every merge; prod on approved promotion (Phase 6) | Ch. 7 |

## Unit IV — Building microservices

| Topic | Where it is covered (docs) | Where it shows up in the build | Basis |
|---|---|---|---|
| Coding standards and developer setup | [coding standards](05-coding-standards.md), [developer setup](08-developer-setup.md) (prerequisites, make targets, ports, troubleshooting) | ✅ Phase 1: `make doctor` / `setup`, ruff + mypy (strict) + pytest, pre-commit with gitleaks, CI workflow; copy-plus-drift-check instead of a shared library | Project |
| Docker | [coding standards §1, §3](05-coding-standards.md), [deployment §2](03-architecture/07-deployment-and-environments.md#2-docker-compose-dev), [developer setup](08-developer-setup.md) | ✅ Phase 1: multi-stage, non-root Dockerfile with HEALTHCHECK per service and for `web`; Docker Compose with `infra` and `app` profiles, healthchecks, `depends_on: service_healthy` and memory limits | Project |
| Cassandra | [ADR-0004](adr/0004-cassandra-event-store.md), [data ownership §4](03-architecture/02-data-ownership.md#4-table-designs) | ✅ Phase 1: single node tuned for a laptop, one keyspace and one login role per owning service, isolation tested. Event store and query tables: Phase 2 | Project |
| Kubernetes | [ADR-0007](adr/0007-kubernetes-kind-and-helm-umbrella.md), [deployment §3](03-architecture/07-deployment-and-environments.md#3-kubernetes-with-kind--helm-umbrella-chart) | kind cluster (via Terraform), probes, rollouts, RBAC (Phase 5) | Ch. 7 (EKS → kind) |
| Endpoint design | [`api/`](../api/README.md), [coding standards §4, §9](05-coding-standards.md#9-api-versioning-rules) | ✅ Phase 1: a `501` stub for every spec operation, generated from the specs, with a contract test per service. Real logic: Phases 2–7 | Syllabus |
| Umbrella project | [coding standards — the umbrella project](05-coding-standards.md#the-umbrella-project-our-interpretation): local (root `docker-compose.yml` + `Makefile`) and Kubernetes (Helm umbrella chart) | ✅ Phase 1: `make up` (root `docker-compose.yml` + `Makefile`). Later: `charts/circuitbreaker` rendered per service by Argo CD (Phases 5, 6) | Project — our interpretation |

## Unit V — Releasing and evolving

| Topic | Where it is covered (docs) | Where it shows up in the build | Basis |
|---|---|---|---|
| Staging environment | [deployment §1](03-architecture/07-deployment-and-environments.md#1-the-path-from-laptop-to-production) | `staging` namespace, Argo CD auto-sync on merge, PostSync smoke tests, chaos rehearsal before promotion (Phase 6) | Project |
| Shipping containers | [deployment §4](03-architecture/07-deployment-and-environments.md#4-cicd-pipeline-github-actions--argo-cd-gitops) | Images tagged by git SHA in GHCR; the same tag promoted to prod by a reviewed commit (Phase 6) | Ch. 7 (continuous delivery of containers) |
| Release process and approvals | [team topology — Release & Reliability](04-team-topology.md#step-4--enabling-and-complicated-subsystem-teams), [ADR-0014](adr/0014-gitops-with-argo-cd.md) | Promotion PRs approved via CODEOWNERS; `git revert` rollbacks (Phase 6) | Ch. 2, Ch. 7 |
| Managing change | [deployment §6](03-architecture/07-deployment-and-environments.md#6-managing-change), [API versioning](05-coding-standards.md#9-api-versioning-rules), [event versioning](03-architecture/03-event-catalog.md#versioning-rules), [ADR process](adr/README.md) | Expand-and-contract migrations, `/v2` side-by-side, Git-revert rollback (Phases 6–8) | Project |
| Measuring microservices transformation progress | [roadmap — Measuring progress](07-roadmap.md#measuring-progress) — DORA four key metrics + incidents auto-remediated, MTTD, architecture metrics | DORA + project metrics dashboard (Phase 8) | Standard — Forsgren, Humble & Kim, *Accelerate* (2018) |

## Beyond the syllabus (clearly marked as extras)

| Extra | Where | Basis |
|---|---|---|
| AI SRE agent with tool allowlist (via OpenRouter) | [SRE agent design](03-architecture/05-sre-agent-design.md) (labelled "Project extension — beyond the textbook"), [ADR-0009](adr/0009-sre-agent-fixed-tool-allowlist.md), [ADR-0013](adr/0013-openrouter-llm-gateway.md) | Project |
| Chaos engineering | [chaos scenarios](03-architecture/06-chaos-scenarios.md) | Project |
| Circuit breaker pattern | [system architecture §3](03-architecture/01-system-architecture.md#3-synchronous-vs-asynchronous-communication) | Standard |

## Viva Notes

**Q: Which syllabus topic is best demonstrated live?**
A: Event sourcing and CQRS through time travel, and independent deployability through a live bad deploy that the agent
rolls back without touching any other service, while Argo CD shows the service going `OutOfSync` and back to `Synced`.

**Q: Which parts of your design come straight from the textbook?**
A: The team topology (Ch. 2's five steps), Event Storming's five phases with competitive analysis, the context-mapping
vocabulary and the Universal Sizing Formula (Ch. 4), and the infrastructure shape: Terraform modules for the cluster
and Argo CD as the GitOps deployment server (Ch. 7). The "Basis" column shows this for every topic.

**Q: Where is the SEED(S) "feedback" step in your project?**
A: Consumers review spec PRs, the UI is built against a mock server generated from the specs before the backend exists,
and contract tests keep code and specs in sync. Phase 0 feedback already changed the design (async cancel, orphan rule).

**Q: How do you measure whether moving to microservices worked?**
A: The DORA four key metrics from *Accelerate* (deployment frequency, lead time, change failure rate, time to restore),
plus mean time to detect, the share of incidents the agent fixed, and architecture metrics such as the share of
changes that touch only one service.
