# 07 — Roadmap

Nine phases, each ending with something that runs (from Phase 1) and a clear **"done when"** check. Owners:
**K** = Kaizer Dewaswala (System Design, Trading, SRE agent), **A** = MD Asif Ali (Market Insights, API Team),
**O** = Mohammad Oweis (Cloud Platform, Release & Reliability).

```mermaid
flowchart LR
    P0["0 Design<br/>and docs"] --> P1["1 Workspace<br/>and skeletons"] --> P2["2 Core trading<br/>(write side)"] --> P3["3 Read side,<br/>web, bots"]
    P3 --> P4["4 Time travel<br/>and leaderboard"] --> P5["5 Kubernetes and<br/>observability"] --> P6["6 IaC and<br/>CI/CD"] --> P7["7 Chaos and<br/>SRE agent"] --> P8["8 Hardening,<br/>metrics, demo"]
```

| Phase | Goal | Main work (owner) | Done when |
|---|---|---|---|
| **0 — Design and documentation** | Agree on what to build and why | All docs, ADRs, OpenAPI specs, service READMEs (all) | All docs written; specs pass `redocly lint` with 0 errors; consistency check passes; every owner has reviewed their study guide; every textbook-verification marker resolved against the textbook chapters or a cited standard source |
| **1 — Developer workspace and skeletons** | Everyone can run everything locally | `make doctor`, Makefile and pre-commit hooks incl. gitleaks (K, O); Docker Compose with Redpanda/Cassandra/Postgres, init jobs for topics and per-service keyspaces/roles, `infra` and `app` profiles (O); a FastAPI skeleton per service with `/healthz`, `/readyz`, `/metrics`, JSON logs, correlation ID and a `501` stub for every spec operation, plus a contract test per service (each owner); gateway proxy and `/v1/system/status` (A); web System Status page (K); CI lint, types, tests, Docker builds and gitleaks; CODEOWNERS (O). *Moved to later phases: `contracts/events/*.schema.json` to Phase 2 (first producers) and the mock server from the specs to Phase 3 (when web needs it).* | Fresh clone → `make doctor` → `make up` → all healthy and the System Status page all green; `make lint` and `make test` pass with 0 errors; each service's contract test passes against its spec; stopping a container turns it red on the status page and restarting turns it green; resource use is measured ([developer setup](08-developer-setup.md#7-resource-use-measured)) |
| **2 — Core trading (write side)** | Orders become trades and balances change correctly | `contracts/events/*.schema.json` and event contract tests (K, A); order-service + matching-engine with event store, snapshots, outbox (K); portfolio accounts, reservations, settlement, orphan release (A); api-gateway sessions/JWT, routing, rate limits, `/internal/` blocking (A) | In Compose: join → place order → match → settle works end to end; matching `domain/` has 100% branch coverage; property test "replay rebuilds the same book" passes; duplicate events don't change balances |
| **3 — Read side, web and bots** | A live, watchable market | mock server generated from the specs (K); market-data candles, depth, trades, WebSockets, `CandleClosed` (A); gateway WebSocket proxy (A); web join/trade/charts/portfolio (K); bot-traders with 4 strategies (K) | 5 phones + 20 bots trade for 10 minutes with no errors; charts update live; spec contract tests green |
| **4 — Time travel and leaderboard** | Show event sourcing to an audience | Q-12 time travel, Q-13 replay stream (A); leaderboard (A); time-travel UI (K) | Time-travel book equals the live book recorded at 3 sampled moments; replay at 10× plays smoothly |
| **5 — Kubernetes and observability** | Production-like platform | Terraform modules `kind-cluster` and `environment`: kind cluster, staging/prod namespaces, quotas, agent RBAC, Secrets (O); Helm umbrella chart with the shared service sub-chart and network policies (O); Prometheus, Grafana, Loki, Alloy, dashboards, alert rules (O); probes and metrics in every service (all) | `make platform` builds the cluster from nothing; `helm install` of the umbrella works in both namespaces; dashboards show golden signals for all 8 services; alert fires on a synthetic error burst |
| **6 — IaC, CI/CD and GitOps** | Repeatable environments and releases | Terraform `argo-cd` module (Argo CD via the Helm provider) + `ApplicationSet` (O); GitHub Actions: build once → tag bump in `deploy/envs/staging/` → Argo CD sync + PostSync smoke tests → `make promote` PR → Release & Reliability approval → Argo CD syncs prod (O); CI create–check–destroy test of `infra/envs/local` (O); optional LocalStack module (O) | A merged PR reaches prod through commits only, with no manual `kubectl` or `helm`; Argo CD shows every Application `Synced` and `Healthy`; `terraform plan` shows no changes; a deliberately failing release is undone by `git revert` and Argo CD re-syncs |
| **7 — Chaos and SRE agent** | Self-healing, explained | chaos-service with 3 scenarios (O); sre-agent loop, evidence collector, OpenRouter client, guardrails, approval, incident feed (K); feed UI (K); rollback's Git step: Argo CD pre-check + revert PR via a GitHub App (K); `main` ruleset and read-only Argo CD account (O); agent + Argo CD coexistence checks (K, O) | In staging, each scenario is fixed by the expected action within 3 minutes of detection, 3 runs in a row; guardrail tests prove out-of-allowlist requests are declined; agent survives LLM outage (do_nothing + escalate); after a `BAD_DEPLOY` rollback the Argo CD Application returns to `Synced` (`git_reconciliation: NOT_NEEDED`), and scale/restart create no `OutOfSync`; in a Git-delivered bad-release rehearsal the rollback opens a `git revert` PR whose merge returns the Application to `Synced` with no new rollout |
| **8 — Hardening, measurement and demo** | Ready for the viva | Load test, runbook, transformation metrics report, demo script and rehearsal (all); pre-recorded clip of the Git-delivered bad release → rollback → `git revert` PR path (K, O) | Two full demo rehearsals without manual fixes; metrics report produced; all viva notes reviewed |

## Phase status

| Phase | Status |
|---|---|
| 0 | 🔒 Design frozen on 2026-11-03 (owners still to review their study guides) |
| 1 | 🟢 Built and verified (2026-11-03); CI green on GitHub |
| 2 | 🟢 Built and verified (2026-11-04): `make demo-trade`; `make test-e2e` (join → trade → settle, `kill -9` of the engine mid-workload with an identical book hash, the fail-closed orphan rule with Redpanda stopped); matching `domain/` at 100% branch coverage; replay and duplicate-event property tests at 1,000 examples in CI and 25,000 in `make test-deep`. Design changes made while building: [ADR-0017](adr/0017-fail-closed-orphan-release.md) (fail-closed orphan release) and self-trade prevention |
| 3–8 | ⚪ Not started |

## Design freeze

The Phase 0 design was **frozen on 2026-11-03**. From now until Phase 7, phases add **code, not design**:

- **Not allowed:** new scope. That means no new services, agent tools, infrastructure components, integrations, or
  ADRs that add capability.
- **Allowed:** fixing errors and inconsistencies found while coding, in the same PR as the code (docs stay the
  source of truth).
- **Allowed:** a superseding ADR only if implementation proves a frozen decision *unworkable*. It replaces the
  decision; it does not extend it.
- Ideas that come up meanwhile go to a "Later" list in the Phase 8 runbook, not into the design.

## Measuring progress

> How do we know the move to microservices is actually working? We measure delivery speed and stability with the
> **DORA "four key metrics"**, then add metrics specific to CircuitBreaker's self-healing goal and to how independent
> the services really are.
>
> **Source:** the four key metrics come from N. Forsgren, J. Humble and G. Kim, *Accelerate: The Science of Lean
> Software and DevOps* (IT Revolution, 2018), the DORA research programme. They are a standard industry source, not
> the textbook. Later DORA reports rename the fourth metric "failed deployment recovery time"; we keep the
> *Accelerate* name.

| Metric | Type | How we measure it | Target by Phase 8 |
|---|---|---|---|
| **Deployment frequency** | DORA | Count of prod promotion merges per week (`git log deploy/envs/prod/`) | ≥ 5 per week across services |
| **Lead time for changes** | DORA | Merge to `main` → Argo CD reports the prod Application `Synced` with that tag | < 30 minutes (excluding approval wait) |
| **Change failure rate** | DORA | Promotions followed by a `git revert` or an incident within 1 hour | < 15% |
| **Time to restore service** | DORA | `IncidentResolved.time_to_recover_seconds` + chaos-service measurements | < 3 minutes for the 3 scenarios |
| **Incidents auto-remediated** | CircuitBreaker | Share of incidents resolved `RECOVERED` after an agent action (approved or not) rather than `ESCALATED` (`sre_agent_incidents_total{resolution}`) | ≥ 90% of chaos runs |
| **Mean time to detect (MTTD)** | CircuitBreaker | Chaos experiment start → `IncidentOpened` (recorded by chaos-service) | < 90 seconds |
| **Independent deploy ratio** | Architecture | % of promotions that changed exactly one service | > 80% |
| **Cross-service change ratio** | Architecture | % of PRs touching more than one `services/*` folder | < 20% |
| **Contract breakages** | Architecture | Contract-test failures caught in CI (good) vs found in staging (bad) | 0 in staging |
| **Shared-data violations** | Architecture | Any service credential with access to another keyspace/DB | 0 (checked by a test) |

## Viva Notes

**Q: What is the order of phases and why?**
A: Workspace first so everyone can run things; then the write side (the source of truth), then read models that depend
on it; then platform, automation, and finally chaos and the agent, which need the platform's metrics and deploy history.

**Q: What does "done when" mean and why have it?**
A: An objective check that a phase is finished — no "almost done". It also tells examiners exactly what to expect.

**Q: Why is the SRE agent so late in the roadmap?**
A: It depends on everything else: metrics and alerts (Phase 5), real rollouts and history (Phases 5–6), and chaos to
test against (Phase 7). Building it earlier would mean mocking all of that.

**Q: What are the DORA metrics, and where do they come from?**
A: Deployment frequency, lead time for changes, change failure rate and time to restore service: two measures of
speed and two of stability, from Forsgren, Humble and Kim's *Accelerate* (2018). We add mean time to detect and the
share of incidents the agent fixed.
