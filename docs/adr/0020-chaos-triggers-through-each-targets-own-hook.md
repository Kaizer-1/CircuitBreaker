# 0020 — Chaos Triggers Through Each Target's Own Hook; a Fourth Scenario That the Agent Must Decline

- **Status:** Accepted. **An exception to the design freeze** ([roadmap](../07-roadmap.md#design-freeze)), made by
  the owner on 2026-11-24 for Phase 7. It adds interfaces (internal chaos hooks on portfolio-service and
  bot-traders, a reset operation) and a scenario; no service, agent tool or infrastructure component is added.
  The traffic spike's trigger was left open by the design on purpose ([chaos scenarios §3.3](../03-architecture/06-chaos-scenarios.md#33--traffic-spike-traffic_spike):
  "a new trigger is an interface change and needs an ADR").
- **Date:** 2026-11-24
- **Deciders:** Kaizer Dewaswala (owner), Mohammad Oweis (Release & Reliability Team), MD Asif Ali

## Context

Phase 7 needs chaos-service to cause four failures on demand, safely, and undo them at once:

1. **Bad deploy** (order-service): designed in Phase 0 as a live patch of the Deployment.
2. **Memory leak** (market-data-service): designed as an internal hook in the target (A-12/A-13).
3. **Traffic spike**: since 2026-11-04 a **panic sell** by the bots' accounts, calibrated to 110 requests/s. The
   design left open how chaos-service starts it: bot-traders had no API, and the panic needs the bots' credentials.
4. **The agent declining to act.** The SRE agent design requires a moment where `OrderServiceCircuitOpen` fires and
   the agent rightly does nothing. Phase 5 triggered it by scaling portfolio-service to 0, but an examiner would then
   ask why the agent did not simply scale it back up (scaling portfolio-service is one of its tools).

And for the demo the owner asked for (5) a **reset** that ends every experiment and undoes every fault, and (6) an
**overlapping** rehearsal (the leak grows while the bad deploy and the panic run), so the audience never waits.

## Decision

**A fault lives in the service it breaks, behind an internal hook only chaos-service can call.** Each target exposes
`POST /internal/v1/faults` and `DELETE /internal/v1/faults`, answering 404 unless the service runs with
`CHAOS_HOOKS_ENABLED=true` (staging and the demo prod), reachable only from chaos-service (NetworkPolicy) and never
routed by api-gateway (it blocks `/internal/`). Nothing about a fault is a metric; switching one is logged at INFO
(`FaultInjected` / `FaultCleared`), which the agent's evidence (error-level logs) never contains.

| Scenario (Chaos Panel button) | Target | How chaos-service causes it | Why there |
|---|---|---|---|
| `BAD_DEPLOY` Bad deploy | order-service | Patches the Deployment (`FAULT_PROFILE=bad-release`): a real new rollout revision | Unchanged from the design |
| `MEMORY_LEAK` Memory leak | market-data-service | A-12/A-13 | Unchanged; A-13 now also gives the memory back, so a reset needs no restart |
| `TRAFFIC_SPIKE` Panic sell | order-service (load through api-gateway) | **A-23/A-24 on bot-traders**: the bots' own accounts dump shares at market, open loop, at the requested rate | The bots' credentials never leave bot-traders; chaos-service holds no trading power |
| `PORTFOLIO_DB_DEGRADED` Portfolio database degraded | portfolio-service's database | **A-21/A-22 on portfolio-service**: a row in its own Postgres makes every reservation wait in Postgres (`pg_sleep`) | Stored in the **data store**, so a restart, rollback or more replicas cannot clear it: the agent's only right answer is `do_nothing` and escalate, and it can say why |

**One experiment per target service at a time** (was: one experiment at a time). Different targets may overlap;
the same target refuses a second with `409 EXPERIMENT_ALREADY_RUNNING`. Each agent incident is still about one
service's symptoms.

**A-20 `POST /v1/chaos/reset`** (admin): ends every running experiment and undoes every fault **by looking at what is
actually running**, not at chaos-service's memory (so it works after chaos-service itself restarted): removes
`FAULT_PROFILE` from order-service, clears the three hooks, and returns order-service to the replica count it had
before a traffic spike (or the default, 2). That last step can undo the agent's scale-up, so it happens only from
the presenter's button, and the incident feed shows it as a "Presenter reset" (the web page merges chaos-service's
own timeline; the agent never reads it).

**Least privilege for chaos-service:** its Kubernetes Role names the one Deployment it may change,
`order-service`, by `resourceNames` (get and patch the Deployment and its scale). Everything else goes through the
hooks. It cannot touch a StatefulSet, so the data stores are out of reach by permission, not only by code.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| **chaos-service sends the panic itself** with the bots' accounts | It would need the bot secret, so chaos-service could trade as any bot; the bots would also see fills they did not cause, from a process they do not know. |
| **The panic as one more bot strategy** (the Phase 3 note) | A strategy decides once per candle and waits for its answers (closed loop): under overload it slows down with order-service and hides the overload it is meant to cause. A crowd does not wait. |
| **Decline scenario = portfolio-service scaled to 0** (Phase 5's trigger) | The agent could fix it by scaling portfolio-service up, which is in its allowlist; "declining" would then look like a mistake. |
| **Decline scenario = a fault inside portfolio-service's process** | A restart would clear it, so again the agent would have a working fix. Only a fault in the data store makes "do nothing and escalate" the genuinely correct answer. |
| **Keep "one experiment at a time"** | Correct for cause and effect, but the leak needs minutes to grow; run alone, the rehearsal would leave the audience waiting. Per-target exclusivity keeps each incident's cause clear. |
| **Reset = stop each experiment in turn (A-11)** | Depends on chaos-service remembering what it started; after its restart a fault could be left behind. |

## Consequences

- ✅ chaos-service holds no secrets and can change one Deployment by name; every other fault is the target's own,
  reversible code, off by default.
- ✅ The decline is a real decision: the cause is outside everything the agent may touch, and its explanation can say
  so in plain English.
- ✅ The panic is the calibrated mix, sent by the accounts that hold the shares.
- ❌ Three more internal operations (portfolio-service A-21/A-22, bot-traders A-23/A-24) and one public one (A-20);
  bot-traders gets its first spec.
- ❌ Overlapping experiments mean two incidents can be open at once (on different services); the feed must keep them
  apart.
- ❌ A reset can undo the agent's scale-up. Accepted: it is a presenter's control for rehearsals, shown on the feed.

## Viva Notes

**Q: Why doesn't chaos-service just generate the traffic spike itself?**
A: The spike is a panic sell, and selling needs the bots' accounts. Giving chaos-service the bot secret would let it
trade as any bot. Instead it asks bot-traders, which already holds those credentials, to panic.

**Q: Why is the "database degraded" fault stored in the database?**
A: So nothing the agent may do can clear it. A restart, a rollback or more replicas all read the same database and
stay slow. The cause is the data store, which the agent may never touch, so doing nothing and escalating is the
right answer, not a failure.

**Q: How do you stop chaos from becoming a backdoor?**
A: Every hook is off unless `CHAOS_HOOKS_ENABLED=true`, reachable only from chaos-service by NetworkPolicy, never
routed by the gateway, and chaos-service's own Kubernetes permission names a single Deployment.

**Q: Why allow overlapping experiments?**
A: A memory leak needs minutes to grow. Running the other scenarios meanwhile keeps the demo moving, and because only
one experiment per service may run, each incident still has one clear cause.
