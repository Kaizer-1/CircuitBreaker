# 0008 — Prometheus, Grafana and Loki for Observability

- **Status:** Accepted
- **Date:** 2026-11-02
- **Deciders:** Mohammad Oweis (owner), Kaizer Dewaswala, MD Asif Ali

## Context

With 8 services, debugging needs metrics and logs in one place. The SRE agent needs machine-readable **metrics**,
**alerts** and **logs** as evidence. Everything must be open source and run in kind.

## Decision

- **Prometheus** scrapes `/metrics` from every service (standard metric names in the coding standards) and evaluates
  **alert rules** (label `remediable=true` for alerts the agent may act on).
- **Loki** stores logs; **Grafana Alloy** collects container logs and ships them to Loki.
- **Grafana** provides dashboards (golden signals per service, market activity, agent activity) with deploy and
  incident annotations.
- Dashboards and alert rules are stored as code in `observability/`.
- Distributed tracing (OpenTelemetry + Tempo) is a **future improvement**; correlation IDs in logs cover the essentials.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| **ELK / OpenSearch for logs** | Powerful full-text search but heavy (JVM, memory) for a laptop; Loki indexes only labels and is much lighter. |
| **Hosted SaaS (Datadog, Grafana Cloud)** | Costs money/needs accounts; less to learn about running the stack. |
| **Promtail as log shipper** | Deprecated in favour of Grafana Alloy. |
| **Full OpenTelemetry tracing now** | Valuable but adds instrumentation and a backend; deferred to keep Phase 5 achievable. |

## Consequences

- ✅ One UI (Grafana) for metrics and logs; same queries for humans and the agent.
- ✅ Alerts as code, reviewed in PRs.
- ❌ More components to run (Prometheus, Loki, Grafana, Alloy).
- ❌ No traces: following one request across services relies on correlation IDs in logs.

## Viva Notes

**Q: What are the golden signals and where do you see them?**
A: Rate, errors, duration (and saturation). Every service exposes `http_requests_total` and
`http_request_duration_seconds`; Grafana shows them per service and Prometheus alerts on them.

**Q: How do you follow one order across services without tracing?**
A: The gateway assigns an `X-Correlation-Id`; every service logs it and every event carries it, so one Loki query shows
the whole journey.

**Q: Why Loki over Elasticsearch?**
A: Loki indexes only labels, not full text, so it's far lighter — suitable for a laptop — and integrates natively with
Grafana.
