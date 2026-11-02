# 0002 — Python 3.12 + FastAPI for All Backend Services

- **Status:** Accepted
- **Date:** 2026-11-02
- **Deciders:** Kaizer Dewaswala, MD Asif Ali, Mohammad Oweis

## Context

Three developers must each be productive across all services, fast. Services need HTTP APIs with OpenAPI, WebSockets,
async I/O (Kafka, Cassandra, Postgres, HTTP calls) and, for the SRE agent, an LLM client. The matching engine needs
predictable, correct logic more than raw speed (a classroom demo is tens to hundreds of orders per second).

## Decision

Use **Python 3.12** and **FastAPI** (with Pydantic v2 models and `asyncio`) for every backend service.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| **Polyglot** (e.g. Go or Rust for matching, Python elsewhere) | Microservices *allow* different languages, but with 3 people a second language doubles tooling, CI and review effort. The engine's speed is not our bottleneck. |
| **Java / Spring Boot** | Mature, but heavier to set up and slower to iterate; the team is more fluent in Python. |
| **Node.js / TypeScript** | Shares a language with the frontend, but weaker fit for the team's Python experience and data tooling. |
| **Flask / Django** | Flask lacks built-in async and OpenAPI generation; Django is heavier and centred on a single relational database. |

## Consequences

- ✅ FastAPI generates OpenAPI from Pydantic models, which contract tests compare against `api/`.
- ✅ Native async suits I/O-heavy services (WebSocket fan-out, consumers).
- ✅ One toolchain (uv, ruff, mypy, pytest) for everything.
- ❌ Python is slower and the GIL limits CPU parallelism in one process — acceptable at our scale; scaling out uses more
  pods, and the matching engine is single-threaded by design anyway.
- ❌ We don't showcase polyglot services; we explain that the architecture would permit it.

## Viva Notes

**Q: Microservices allow any language — why only one?**
A: Allowed is not required. For a 3-person team one language minimises cognitive load and tooling. If a service ever
needed it, it could be rewritten in another language without affecting others, because contracts are language-neutral.

**Q: Is Python fast enough for a matching engine?**
A: For hundreds of orders per second, yes. The bottleneck is I/O, not CPU. A real exchange would use C++/Rust, but
the architecture (single writer per ticker, event store) would be the same.

**Q: Why FastAPI over Flask?**
A: Built-in async, request validation from type hints, and automatic OpenAPI generation, which supports our
spec-first workflow.
