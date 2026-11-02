# 0013 — OpenRouter as the SRE Agent's LLM Gateway

- **Status:** Accepted — amends [ADR-0009](0009-sre-agent-fixed-tool-allowlist.md) (which originally named the Claude
  API as the LLM provider)
- **Date:** 2026-11-02
- **Deciders:** Kaizer Dewaswala (owner), MD Asif Ali, Mohammad Oweis

## Context

The SRE agent needs an LLM that supports **tool (function) calling** to choose one of its four allowlisted actions.
The original plan was to call the Claude API directly. The team decided instead to use **OpenRouter**, a hosted
gateway that exposes many models (Claude, GPT, Llama, Mistral, …) through one OpenAI-compatible API and one key.

## Decision

The agent calls the LLM through **OpenRouter's OpenAI-compatible chat-completions API**, behind a small internal
`LLMClient` interface with one implementation (`OpenRouterClient`):

- Model chosen by configuration: `SRE_AGENT_MODEL` (an OpenRouter model slug; default a Claude Sonnet model, e.g.
  `anthropic/claude-sonnet-5.5` — confirm the exact slug on OpenRouter), optional `SRE_AGENT_FALLBACK_MODELS`.
- The chosen model **must support tool calling**; requests ask OpenRouter to route only to providers that support all
  request parameters.
- `tool_choice: "auto"`, `parallel_tool_calls: false`, a system-prompt rule to call exactly one tool, and **our own
  JSON Schema validation** of the returned arguments. A reply without exactly one valid tool call → `do_nothing` +
  escalate.
- `OPENROUTER_API_KEY` from Secrets Manager (LocalStack) → Kubernetes Secret; the key has a **credit limit**.
- Only minimised evidence is sent (no nicknames or trader IDs).

Everything in ADR-0009 (four tools, guardrails, RBAC, approval toggle) is unchanged.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| **Claude API directly** (original plan) | Excellent tool calling, but ties the agent to one vendor's API and key; switching or comparing models would need code changes. |
| **Another single vendor directly** | Same lock-in problem. |
| **Self-hosted open model** (e.g. via Ollama) | No external dependency, but a laptop cannot run a model that is reliable at tool calling alongside the whole cluster. |
| **No LLM (rules only)** | Loses plain-English diagnosis — see ADR-0009. |

## Consequences

- ✅ Model choice is configuration: we can compare models for diagnosis quality and cost without code changes.
- ✅ Fallback models give resilience if one provider is down.
- ✅ One OpenAI-compatible format, widely documented; the `LLMClient` interface keeps the rest of the agent independent
  of it (an Anti-Corruption Layer).
- ❌ An extra hop and an extra third party: prompts (evidence) pass through OpenRouter and the model provider —
  mitigated by data minimisation.
- ❌ Feature support (tool calling, strict schemas, forced tool choice) **varies by model and provider**, so we validate
  arguments ourselves and never rely on forced tool choice.
- ❌ Another account, key and bill to manage — mitigated by the key's credit limit and token metrics.

## Viva Notes

**Q: Why OpenRouter instead of calling one LLM provider directly?**
A: One API and key for many models, so the model is a configuration choice and we can fall back to another model if
one is unavailable. The `LLMClient` interface keeps the agent independent of the gateway itself.

**Q: Does using OpenRouter make the agent less safe?**
A: No. Safety comes from our side: four tools, schema validation in our code, guardrails, RBAC and optional human
approval. The LLM only *suggests*; it never acts directly.

**Q: What data leaves your system?**
A: Only the evidence for the diagnosis — metrics, truncated error logs with nicknames and trader IDs removed, and deploy
history. No orders, balances or secrets.
