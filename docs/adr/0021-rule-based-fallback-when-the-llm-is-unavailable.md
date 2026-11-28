# 0021 — A Rule-Based Fallback When the LLM Is Unavailable

- **Status:** Accepted. **Amends [ADR-0009](0009-sre-agent-fixed-tool-allowlist.md)** ("LLM failure of any kind →
  `do_nothing` + escalate", guardrail G9). **An exception to the design freeze**
  ([roadmap](../07-roadmap.md#design-freeze)), made by the owner on 2026-11-24 for Phase 7. It adds no tool, no
  service and no infrastructure: the same four tools, the same guardrails, a second way to choose among them.
- **Date:** 2026-11-26
- **Deciders:** Kaizer Dewaswala (owner), Mohammad Oweis (Release & Reliability Team), MD Asif Ali

## Context

The SRE agent asks a model, through OpenRouter, to choose one of four tools ([ADR-0009](0009-sre-agent-fixed-tool-allowlist.md),
[ADR-0013](0013-openrouter-llm-gateway.md)). That call can fail in many ways: no internet (the demo may run on a
phone hotspot, and the cluster is proven to run offline), OpenRouter or the provider down or slow, the key's credit
used up, a refusal, a reply with no tool call or with arguments that are not valid JSON. ADR-0009 answered every one
of them with `do_nothing` and escalate. That is safe, but then the demo's self-healing depends on a remote service,
and a failure the agent can plainly recognise is left to a person.

A naive set of rules would be dangerous in a different way. "A deploy happened recently, so roll back" is wrong right
after the agent's **own** rollback, which is itself a new rollout: an overload in the minutes after it would be "fixed"
by rolling back again, onto the bad release. Errors must be told apart by **what they are**, not by when the last
rollout happened (the owner's direction, 2026-11-24).

## Decision

**When the model gives no usable tool call, rules choose instead, labelled "rule-based (LLM unavailable)", and their
proposal goes through the same guardrails, budgets and approval mode as the model's.**

| The model's answer | What happens |
|---|---|
| One tool call with arguments that parse as JSON | The **guardrails** judge it, as before (an unknown tool is G1, a data store is G2, too many replicas is G3, and so on). A declined proposal is declined; the rules do not get a second try |
| No answer within the **decision deadline** (25 s for every attempt together; one retry, only after a fast failure such as a refused connection, HTTP 5xx or 429), the call budget used up (60 model calls per hour), a refusal, a content filter, a cut-off reply, no tool call, several tool calls, arguments that are not JSON | The **rules** below choose. The feed says so, with the reason ("OpenRouter did not answer in 25 s") |
| An alert labelled `remediable=false` | Neither: a fixed explanation and an escalation, as before ([SRE agent design §3](../03-architecture/05-sre-agent-design.md#alerts-the-agent-must-not-act-on)) |

**Three facts, computed by the evidence collector, never by a model.** The rules read them, and the model's evidence
carries the same three, so both paths reason from the same distinctions:

| Fact | From | Values |
|---|---|---|
| **Error type** | `http_problems_total{code}` (every 5xx counted by its Problem Details code, 7a) | `INTERNAL_ERROR` (a bug), `OVERLOADED` (the 50-request limit), `DEPENDENCY_UNAVAILABLE` (an open breaker, or the gateway's upstream not answering); for the gateway also the share of its 5xx it only **passed on** from an upstream |
| **Revision** | The Deployment's ReplicaSets, and each one's ready pods over the last 12 hours (Prometheus, kube-state-metrics) | **Known-good**: this pod template once **ran 15 minutes or more in a row** (a restart's ReplicaSets count together: only the restart annotation differs, which is ignored), as after the agent's rollback to a release that ran for hours. **New**: it never did: a fresh release, or a bad one deployed again. *Not* the ReplicaSet's age (found on staging, 2026-11-26): Kubernetes reuses a template's old ReplicaSet whenever that template is deployed again, so the day's second chaos bad deploy looked hours old and "known-good", and was left alone. A release last run more than 12 hours ago (Prometheus's retention) counts as new |
| **Load** | `http_requests_total` | The request rate now against its median over the hour that ended 15 minutes ago (≥ 2× is "far above normal"). No baseline while that median is under 0.5 requests a second (after a rebuild): then only a CPU at 80% or more of its limit is "far above normal", and the explanation says "no traffic baseline yet" (2026-11-28) |

**The rules, in order** (`services/sre-agent/src/sre_agent/domain/fallback.py`, each one a unit test):

| Evidence | Proposal |
|---|---|
| Memory above 70% of its limit and rising (`MarketDataMemoryGrowth`) | New revision: roll back. Otherwise: restart |
| An error or latency alert whose errors are `DEPENDENCY_UNAVAILABLE`, an open breaker, or (gateway) passed on from an upstream | `do_nothing`: the cause is elsewhere; escalate |
| Errors `OVERLOADED`, or latency with load far above normal | Scale up by up to 3 (at most 5), **whatever the revision** |
| Errors `INTERNAL_ERROR` on a **new** revision | Roll back |
| Errors `INTERNAL_ERROR` on a known-good revision | `do_nothing`: rolling back would not help; escalate |
| `PodCrashLooping` | New revision: roll back. Otherwise `do_nothing` and escalate |
| `OrderServiceOverProvisioned` | Scale back toward 2 |
| Anything else | `do_nothing` and escalate: no rule guesses |

**The pair the tests and the evaluation must hold** (owner, 2026-11-24): overload shortly after a rollback is scaled,
**never** rolled back; internal errors after a bad release are rolled back. The first half is also a guardrail, so it holds
whoever proposes: G7 refuses to roll back a release that is known-good (SRE agent design §5). The evaluation measures
how often the model gets it right by itself.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| **Keep ADR-0009's `do_nothing` + escalate** | Safe, but a lost internet connection or a used-up credit would stop all self-healing during the demo, for failures the agent can recognise from its own metrics (the owner's decision) |
| **Rules only, no model** | Rejected in ADR-0009 and still rejected: no plain-English reasoning, nothing for evidence that fits no rule. The rules are the backup, not the agent |
| **A local model on the laptop** | Several GB of RAM on a laptop already near its budget (deployment doc §3), and much slower |
| **A second model on OpenRouter** (`SRE_AGENT_FALLBACK_MODELS`) | Kept as an option, but it needs the same internet and the same key, so it does not cover the failures that matter most here |
| **Rules keyed on "a recent deploy"** | Rolls back again after the agent's own rollback (the Context). Replaced by the three facts |

## Consequences

- ✅ The three injected failures (bad deploy, leak, load) are still fixed without the internet, within every
  guardrail, and the feed says honestly which path chose.
- ✅ The model is no less safe than before: its proposals meet the same guardrails, and its evidence now states the
  error type, revision and load explicitly.
- ✅ Deterministic and fully tested; the evaluation set runs the same cases through both paths.
- ❌ The rules know only the patterns above; anything else is escalated, as ADR-0009 did for every failure.
- ❌ Two decision paths to keep consistent. Mitigated by one source of facts (the collector) and evaluation pairs.
- ❌ Rule explanations are templates: clear, but less specific than the model's.

**Note (2026-11-28, from the 7c staging review): two labels, and one more try.** The staging feed said "Rule-based
(LLM unavailable: the model answered with no tool call (finish: tool_calls))" on the leak incident. The model *had*
answered: OpenRouter's record of that generation shows Bedrock's own stop reason `tool_use` and 322 completion
tokens, but the answer the provider "Amazon Bedrock" sent back held only the model's opening sentence, no tool
call. So the agent now tells the two failures apart (owner: "LLM unavailable" only when the model really didn't
answer):

| What came back | Label on the feed | Before the rules decide |
|---|---|---|
| Nothing usable from OpenRouter: timeout, refused key, no credit, moderation, unreachable | `Rule-based (LLM unavailable: <reason>)` | One retry after a fast failure (as before) |
| An answer without exactly one valid tool call: text only, a call reported but not sent, several calls, arguments that are not JSON, a cut-off reply | `Rule-based (malformed model answer: <reason>)` | **Asked once more, never from the same provider** (`provider.ignore`), if 8 s of the 25 s deadline are left |

Forcing the tool call instead (`tool_choice: "required"`) is impossible: Claude Sonnet 5.5 refuses forced tool use
("tool_choice: type tool and any are not supported for this model"), and with strict routing the request finds no
provider at all (HTTP 404). The evaluation run with "required" failed every case before anything reached staging,
which is why it stays "auto". The retried answer is the model's decision, recorded as the model's; the evaluation
counts answers that needed a second try (`model_runs_on_second_try`) and fails if any run ends without a usable
answer.

## Viva Notes

**Q: Isn't a rule-based fallback exactly the "if-else ops script" the LLM was meant to replace?**
A: It is a safety net, not the agent. The model still decides whenever it can, and explains in its own words. The
rules cover only the failures we can recognise with certainty from our own metrics; for anything else they do
nothing and call a person, which is what ADR-0009 did for every failure.

**Q: The agent just rolled back a bad release, and now order-service is overloaded. Why doesn't the fallback roll back
again, since a deploy just happened?**
A: Because it never asks "did a deploy just happen?". It asks what the errors are (`OVERLOADED`, not
`INTERNAL_ERROR`) and whether the running pod template is new. After a rollback, Kubernetes runs the old ReplicaSet
again, so the revision is known-good. Overload on a known-good revision means scale. A test and an evaluation case
hold exactly this pair.

**Q: How does the incident feed show which path decided?**
A: Every proposal records its source: the model's name, "rule-based (LLM unavailable)" with the reason, such as
"OpenRouter did not answer in 25 s", or "rule-based (malformed model answer)" when the model answered without one
usable tool call.

**Q: Why not retry the model until it answers?**
A: Time matters more than the model's wording. Mitigation should start within 30 s of detection, so all attempts
together get 25 s, with one retry only after a fast failure.
