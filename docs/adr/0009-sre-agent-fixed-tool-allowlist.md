# 0009 — SRE Agent with a Fixed Tool Allowlist

- **Status:** Accepted — LLM provider amended by [ADR-0013](0013-openrouter-llm-gateway.md) (OpenRouter instead of
  calling the Claude API directly)
- **Date:** 2026-11-02
- **Deciders:** Kaizer Dewaswala (owner), Mohammad Oweis, MD Asif Ali

## Context

We want an AI agent that diagnoses and fixes incidents live and explains itself. LLMs can be wrong, can be manipulated
by text in their input (prompt injection: e.g. a crafted log line), and act fast. An agent with broad powers (shell,
`kubectl`, database access) could turn a small incident into a disaster.

## Decision

The agent uses an LLM with **tool calling** and has **exactly four tools**: `rollback_deployment`,
`scale_deployment` (1–5 replicas), `restart_deployment`, `do_nothing`. Evidence (metrics, logs, deploy history) is
collected by deterministic code, not by LLM tools. Every proposal passes **code guardrails** (service allowlist per
tool, replica bounds, 5-minute cooldown, max 3 actions per incident and 6 per hour, pause switch). The agent's
Kubernetes **ServiceAccount** can only read/patch Deployments, scale and read ReplicaSets/Pods in its own namespace.
An **approval toggle** requires a human to approve each action (on by default in prod). LLM failure of any kind →
`do_nothing` + escalate. The tool list is changed only by code review — **never at runtime and never by the LLM**.
Details: [05-sre-agent-design.md](../03-architecture/05-sre-agent-design.md).

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| **General shell / `kubectl` tool** | Unbounded blast radius; prompt injection could do anything the credentials allow. |
| **Read tools for the LLM (query Prometheus/Loki itself)** | More "agentic", but widens the attack surface and makes runs less reproducible; fixed evidence is enough for our three failure types. Possible future extension (read-only, still allowlisted). |
| **Pure rule-based remediation (no LLM)** | Safe and predictable, but cannot explain itself in plain English or handle evidence that doesn't fit a rule. We still use rules — as guardrails around the LLM. |
| **Human-only response** | Doesn't demonstrate self-healing; slower MTTR. Kept as the approval mode and escalation path. |
| **Kubernetes HPA for scaling** | Handles only one failure type and would fight the agent over replicas. |

## Consequences

- ✅ Worst-case outcome of a bad LLM answer is one bounded, reversible action.
- ✅ Defence in depth: tools → schemas → guardrails → RBAC → optional human.
- ✅ Explanations make every action auditable on the incident feed.
- ❌ The agent cannot fix problems that need other actions (e.g. a full disk) — it escalates instead.
- ❌ Depends on an external LLM API (availability, cost, data leaving our system) — mitigated by fallback to
  `do_nothing`, data minimisation and a spend limit (ADR-0013).

## Viva Notes

**Q: Why only four tools?**
A: They cover the three failure types we inject (bad deploy → rollback, leak → restart, load → scale) plus a safe
"no action". A small, fixed set means the worst case is known and bounded.

**Q: What if the LLM is tricked by a malicious log line?**
A: It can only pick among four tools, with arguments validated against schemas and guardrails, under RBAC that forbids
anything else. The worst case is one allowed action within limits.

**Q: Could someone add a tool at runtime?**
A: No. The allowlist is in code, changed only through a reviewed pull request; there is no API to change tools or model.
