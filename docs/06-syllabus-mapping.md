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
| Reducing coordination costs | [service sizing](02-domain/03-service-sizing.md), [team topology](04-team-topology.md#how-this-reduces-coordination), [event catalog](03-architecture/03-event-catalog.md) (contracts instead of meetings) | CODEOWNERS, CI that builds and deploys only the services whose folder changed, one Argo CD Application per service, versioned contracts (Phases 1, 6) | Ch. 2, Ch. 4 |
| Operating model and Team Topologies | [team topology](04-team-topology.md) — the textbook's 5 design steps; System Design, microservices team template (Trading, Market Insights), Cloud Platform, Release & Reliability, API Team; 4 team types, 3 interaction modes, team-size principle | Ownership labels on alerts and dashboards (Phase 5); CODEOWNERS on `deploy/envs/prod/` for the release team (Phase 6) | Ch. 2 |
| SEED(S): actors | [01-actors](01-seeds/01-actors.md) | — | Syllabus |
| SEED(S): jobs to be done | [02-jobs-to-be-done](01-seeds/02-jobs-to-be-done.md) | — | Syllabus |
| SEED(S): sequence diagrams | [03-sequence-diagrams](01-seeds/03-sequence-diagrams.md) (6 diagrams) | ✅ Phase 2: `make demo-trade` and `make test-e2e` follow diagrams 1–4 and 7 through the gateway. ✅ Phase 3: the web join and trade pages follow diagrams 1, 2 and 4 from a phone (Phases 4–7: the rest) | Syllabus |
| SEED(S): actions and queries | [04-actions-and-queries](01-seeds/04-actions-and-queries.md) (A-01…A-19, Q-01…Q-23) | ✅ Phase 2: A-01–A-09 (A-08 and A-09 are event-driven), A-19, Q-01, Q-07–Q-09, Q-14–Q-16. ✅ Phase 3: Q-02–Q-06 (market data, REST and WebSocket) and Q-10 (portfolio WebSocket) | Syllabus |
| SEED(S): open-standard spec | [`api/`](../api/README.md) — 7 OpenAPI 3.1 specs, lint-clean; events as JSON Schemas in the [event catalog](03-architecture/03-event-catalog.md) | Contract tests against the specs (Phase 1 onwards); ✅ Phase 2: `contracts/events/*.schema.json` generated from the catalog, every produced event validated against them | Syllabus |
| SEED(S): feedback | [`api/README.md` — Feedback](../api/README.md#feedback-seeds-step-6) | ✅ Phase 3: `make mock` serves the public API from the specs: Prism with the owning services' examples, plus a WebSocket mock for Q-06 and Q-10; `npm run dev:mock` runs the web app against it, with no backend | Syllabus |
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
| Event Sourcing | [ES and CQRS](03-architecture/04-event-sourcing-and-cqrs.md), [ADR-0005](adr/0005-event-sourcing-for-matching-engine.md) | ✅ Phase 2: matching-engine event store in Cassandra (logged batches), snapshots every 500 events, books rebuilt from the store on every start; property tests: replay from any snapshot gives the same book hash; `kill -9` → same hash ([how matching works](../services/matching-engine/docs/how-matching-works.md)). ✅ Phase 4: time travel (Q-12) rebuilds any moment from snapshot + events and proves it against the live engine's state hash; replay (Q-13) at up to 60×; e2e rebuilds three recorded moments exactly ([ES and CQRS §5](03-architecture/04-event-sourcing-and-cqrs.md#5-time-travel-and-replay)) | Standard |
| CQRS | [ES and CQRS](03-architecture/04-event-sourcing-and-cqrs.md), [ADR-0006](adr/0006-cqrs-portfolio-read-model.md) | ✅ Phase 2: portfolio-service settles from `matching.v1` (idempotent, `processed_events` in the same transaction); order-service keeps order status from the same events. ✅ Phase 3: market-data-service builds candles, depth, trades and last price from `matching.v1` (gap fill from the engine's Q-14/Q-15, deterministic `CandleClosed` IDs); WebSocket clients get a snapshot, then updates in sequence; portfolio Q-10 pushes after each commit via Postgres LISTEN/NOTIFY ([read side](03-architecture/08-read-side.md)). ✅ Phase 4: time travel is a query on the read side (market-data) that never touches live state; leaderboard Q-11 from portfolio's read model | Standard |

## Unit III — DevOps and infrastructure

| Topic | Where it is covered (docs) | Where it shows up in the build | Basis |
|---|---|---|---|
| DevOps principles | [deployment §4](03-architecture/07-deployment-and-environments.md#4-cicd-pipeline-github-actions--argo-cd-gitops) (build once, deploy many; everything in Git; pull, don't push; fast feedback), [coding standards §11](05-coding-standards.md#11-git-workflow-and-commit-messages) (trunk-based) | ✅ Phase 6 (PR 6b): GitHub Actions builds each changed image once (amd64 and arm64), scans it (Trivy), pushes it to GHCR and, after every check passed, commits its tag to staging, where Argo CD deploys it; `main` changes only through pull requests (ruleset); team runs what it builds | Ch. 7 (CD vs deployment); Project |
| IaC environment and pipeline | [deployment §5](03-architecture/07-deployment-and-environments.md#5-infrastructure-as-code-terraform), [ADR-0015](adr/0015-terraform-local-platform.md) (amends [ADR-0010](adr/0010-terraform-localstack.md)) — Terraform modules `kind-cluster`, `environment`, `argo-cd`, optional `localstack`, called from `envs/local` | ✅ Phase 6 (PR 6a): `make platform` builds the cluster, both environments (quota, default-deny policy, the agent's Role), the platform charts, Sealed Secrets and Argo CD from nothing; pinned providers with a committed lock file; local, git-ignored state (it holds secrets); CI `terraform fmt -check` + `validate`. The create–check–destroy job in CI: PR 6c | Ch. 7 |
| Infrastructure components | [deployment §5 — mapping to the textbook](03-architecture/07-deployment-and-environments.md#how-this-maps-onto-the-textbooks-aws-infrastructure) (network → Kubernetes → GitOps server), [system architecture](03-architecture/01-system-architecture.md) (container inventory), [ADR-0003](adr/0003-redpanda-event-bus.md), [ADR-0004](adr/0004-cassandra-event-store.md), [ADR-0008](adr/0008-observability-stack.md) | ✅ Phase 1: Redpanda, Cassandra, Postgres in Compose. ✅ Phase 5: a kind cluster with a local image registry; Traefik ingress; Redpanda, Cassandra, Postgres as StatefulSets with volumes; Prometheus, Grafana, Loki, Alloy ([ADR-0018](adr/0018-kubernetes-platform-components.md): each chosen for licence and maintenance, measured RAM budget). ✅ Phase 6: all of it built by Terraform, plus Argo CD and the Sealed Secrets controller | Ch. 7 |
| GitOps deployment server | [ADR-0014](adr/0014-gitops-with-argo-cd.md), [deployment §4](03-architecture/07-deployment-and-environments.md#4-cicd-pipeline-github-actions--argo-cd-gitops) — Argo CD, automated sync, self-heal off, one Application per service | ✅ Phase 6 (PR 6a): an ApplicationSet makes one Application per service, data store and ingress per environment (29 in all), each with its own paths; tested that a scale and a restart by hand are never undone, a changed pod template is reported, an unrelated commit leaves a drifted service alone, and a pushed commit reaches the cluster in about a minute (measured). CI commits each new image tag to staging, one commit per service (PR 6b); promotion to prod: PR 6c | Ch. 7 |
| Secret management (also the owner's Kubernetes-security course) | [ADR-0019](adr/0019-sealed-secrets-for-gitops.md), [deployment §5](03-architecture/07-deployment-and-environments.md#secrets-sealing-backing-up-the-key-rotating) | ✅ Phase 6: every service secret is a SealedSecret in Git (strict scope, random values), the controller pinned by digest, the sealing key backed up off the laptop with a tested restore drill, rotation procedures; gitleaks allowlists only the sealed folders and a check fails on any plaintext there | Kubernetes documentation; project |

## Unit IV — Building microservices

| Topic | Where it is covered (docs) | Where it shows up in the build | Basis |
|---|---|---|---|
| Coding standards and developer setup | [coding standards](05-coding-standards.md), [developer setup](08-developer-setup.md) (prerequisites, make targets, ports, troubleshooting) | ✅ Phase 1: `make doctor` / `setup`, ruff + mypy (strict) + pytest, pre-commit with gitleaks, CI workflow; copy-plus-drift-check instead of a shared library | Project |
| Docker | [coding standards §1, §3](05-coding-standards.md), [deployment §2](03-architecture/07-deployment-and-environments.md#2-docker-compose-dev), [developer setup](08-developer-setup.md) | ✅ Phase 1: multi-stage, non-root Dockerfile with HEALTHCHECK per service and for `web`; Docker Compose with `infra` and `app` profiles, healthchecks, `depends_on: service_healthy` and memory limits | Project |
| Cassandra | [ADR-0004](adr/0004-cassandra-event-store.md), [data ownership §4](03-architecture/02-data-ownership.md#4-table-designs) | ✅ Phase 1: single node tuned for a laptop, one keyspace and one login role per owning service, isolation tested. ✅ Phase 2: matching-engine event store and order-service tables (outbox, lightweight transaction for Idempotency-Key, per-column writes), integration-tested with testcontainers. Event store and query tables: Phase 2 | Project |
| Kubernetes | [ADR-0007](adr/0007-kubernetes-kind-and-helm-umbrella.md), [deployment §3](03-architecture/07-deployment-and-environments.md#3-kubernetes-with-kind--helm-umbrella-chart) | ✅ Phase 5: one-node kind cluster (`make k8s-up`; ✅ Terraform in Phase 6); liveness/readiness/startup probes; requests and limits from measured use; rolling updates with no failed request (preStop); Recreate for single writers; ConfigMaps and Secrets; NetworkPolicies proven enforced; Ingress for phones; replicas scaled by hand to show capacity (no HPA: the agent scales in Phase 7). ✅ Phase 6: the agent's Role (Deployments get/list/patch, scale, ReplicaSets and Pods read; nothing else) and ResourceQuotas per namespace; the agent itself: Phase 7 | Ch. 7 (EKS → kind) |
| Endpoint design | [`api/`](../api/README.md), [coding standards §4, §9](05-coding-standards.md#9-api-versioning-rules) | ✅ Phase 1: a `501` stub for every spec operation, generated from the specs, with a contract test per service. ✅ Phase 2: real routes for the gateway (sessions, roles, rate limits), order-service, portfolio-service and the matching-engine reads. ✅ Phase 3: market-data REST and WebSockets, the gateway's WebSocket proxy (per-channel roles, the trader's own stream only); the rest in Phases 4–7 | Syllabus |
| Umbrella project | [coding standards — the umbrella project](05-coding-standards.md#the-umbrella-project-our-interpretation): local (root `docker-compose.yml` + `Makefile`) and Kubernetes (Helm umbrella chart) | ✅ Phase 1: `make up` (root `docker-compose.yml` + `Makefile`). ✅ Phase 5: the Helm umbrella chart `charts/circuitbreaker` (one generic service sub-chart under 9 aliases, plus the data stores), installed whole by `make k8s-up` and renderable one service at a time (`global.only`) for Phase 6's per-service Argo CD Applications | Project — our interpretation |

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
| Circuit breaker pattern | [system architecture §3](03-architecture/01-system-architecture.md#3-synchronous-vs-asynchronous-communication); ✅ Phase 2: order-service → portfolio-service, metrics `circuit_breaker_state` and `circuit_breaker_trips_total` ([order-service README](../services/order-service/README.md#circuit-breaker-shown-in-the-demo)) | Standard |
| Transactional outbox, idempotent consumers, fail-closed release | [ES and CQRS §4](03-architecture/04-event-sourcing-and-cqrs.md#the-outbox-in-order-service), [ADR-0017](adr/0017-fail-closed-orphan-release.md); ✅ Phase 2: proven by property tests and by `make test-e2e` with Redpanda stopped | Standard / Project |

## Viva Notes

**Q: Which syllabus topic is best demonstrated live?**
A: Event sourcing and CQRS through time travel, and independent deployability through a live bad deploy that the agent
rolls back without touching any other service, while Argo CD shows the service going `OutOfSync` and back to `Synced`.

**Q: Which parts of your design come straight from the textbook?**
A: The team topology (Ch. 2's five steps), Event Storming's five phases with competitive analysis, the context-mapping
vocabulary and the Universal Sizing Formula (Ch. 4), and the infrastructure shape: Terraform modules for the cluster
and Argo CD as the GitOps deployment server (Ch. 7). The "Basis" column shows this for every topic.

**Q: Where is the SEED(S) "feedback" step in your project?**
A: Consumers review spec PRs, the UI can run against a mock server generated from the specs (`make mock`) with no
backend, and contract tests keep code and specs in sync. Phase 0 feedback already changed the design (async cancel,
orphan rule). Building Phase 2 changed it again (the fail-closed orphan rule, ADR-0017; self-trade prevention), and so
did Phase 3 (`recent_trades` in the Q-06 snapshot, so a reconnecting client rebuilds its trades tape).

**Q: How do you measure whether moving to microservices worked?**
A: The DORA four key metrics from *Accelerate* (deployment frequency, lead time, change failure rate, time to restore),
plus mean time to detect, the share of incidents the agent fixed, and architecture metrics such as the share of
changes that touch only one service.
