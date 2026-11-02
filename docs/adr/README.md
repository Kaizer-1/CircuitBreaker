# Architecture Decision Records (ADRs)

> **ADR:** a short document that records **one** important decision: the situation (*context*), what we chose
> (*decision*), what else we considered (*alternatives*), and what follows from it (*consequences*). ADRs are never
> edited to change history; a reversed decision gets a new ADR that **supersedes** the old one.

| # | Decision | Status |
|---|---|---|
| [0001](0001-microservices-over-monolith.md) | Microservices over a monolith | Accepted |
| [0002](0002-python-fastapi.md) | Python 3.12 + FastAPI for all backend services | Accepted |
| [0003](0003-redpanda-event-bus.md) | Redpanda as the event bus | Accepted |
| [0004](0004-cassandra-event-store.md) | Cassandra as the event store | Accepted |
| [0005](0005-event-sourcing-for-matching-engine.md) | Event sourcing for the matching engine | Accepted |
| [0006](0006-cqrs-portfolio-read-model.md) | CQRS with a Postgres portfolio read model | Accepted |
| [0007](0007-kubernetes-kind-and-helm-umbrella.md) | Kubernetes (kind) with a Helm umbrella chart; staging and prod namespaces | Accepted |
| [0008](0008-observability-stack.md) | Prometheus, Grafana and Loki for observability | Accepted |
| [0009](0009-sre-agent-fixed-tool-allowlist.md) | SRE agent with a fixed tool allowlist | Accepted (LLM provider amended by 0013) |
| [0010](0010-terraform-localstack.md) | Terraform against LocalStack | Accepted |
| [0011](0011-synchronous-reservation-over-saga.md) | Synchronous reservation instead of an asynchronous saga | Accepted |
| [0012](0012-prices-in-integer-paise.md) | Money and prices as integer paise | Accepted |
| [0013](0013-openrouter-llm-gateway.md) | OpenRouter as the SRE agent's LLM gateway | Accepted |

**Template:** `# NNNN — Title` · Status · Date · Deciders · Context · Decision · Alternatives considered ·
Consequences (positive / negative) · Viva Notes.

## Viva Notes

**Q: Why write ADRs?**
A: Months later nobody remembers *why* a choice was made. ADRs keep the reasoning and the rejected options next to the
code, so new team members (and examiners) can understand and challenge decisions without guesswork.

**Q: What do you do when a decision changes?**
A: Write a new ADR that supersedes the old one and link them both ways. Example: ADR-0013 changed the SRE agent's LLM
access from the Claude API directly to OpenRouter, amending ADR-0009.

**Q: Which ADR would you defend first in an interview?**
A: ADR-0011 (sync reservation vs saga): it shows a real trade-off between consistency, availability and complexity.
