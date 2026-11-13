# 0018 — Kubernetes Platform Components: Ingress, Data Stores, Monitoring and Logs

- **Status:** Accepted. Records choices that [ADR-0007](0007-kubernetes-kind-and-helm-umbrella.md),
  [ADR-0008](0008-observability-stack.md) and the [deployment doc](../03-architecture/07-deployment-and-environments.md)
  left to Phase 5 ("we decide in Phase 5 whether to use community charts or minimal in-repo templates"). It adds no
  capability: the components are the ones those documents already name, plus the ingress controller every
  Kubernetes deployment needs.
- **Date:** 2026-11-13
- **Deciders:** Mohammad Oweis (owner, Cloud Platform Team), Kaizer Dewaswala, MD Asif Ali

## Context

Phase 5 runs the whole system on a local kind cluster. Five pieces had to be chosen: how traffic gets in, how the
data stores run, which Prometheus and Grafana distribution, which Loki chart, and which log collector. Two facts
from 2025–2026 shaped the choices, so we checked current sources before deciding:

- **ingress-nginx is retired.** Kubernetes SIG Network retired the community ingress-nginx controller in March 2026:
  no more releases or security fixes ([Kubernetes blog, "Ingress NGINX Retirement"](https://www.kubernetes.io/blog/2025/11/11/ingress-nginx-retirement/)).
- **Bitnami's free images are gone.** In August 2025 Bitnami moved its public images to an unmaintained
  "legacy" repository; current images need a paid subscription ([Docker blog, "Broadcom's New Bitnami
  Restrictions"](https://www.docker.com/blog/broadcoms-new-bitnami-restrictions-migrate-easily-with-docker/)).
  Many popular charts (including most Cassandra and Postgres charts) are Bitnami's.
- **The Loki chart moved.** Since March 2026 the open-source Loki chart is published by `grafana-community`; the
  `grafana/loki` chart is for Grafana Enterprise Logs only ([Grafana Loki docs, "Upgrade to the community
  chart"](https://grafana.com/docs/loki/latest/setup/upgrade/upgrade-to-community/)). The Grafana chart moved the
  same way, and kube-prometheus-stack now uses it.
- **Promtail is deprecated** in favour of Grafana Alloy (already recorded in ADR-0008).

## Decision

| Piece | Choice (pinned) | Why |
|---|---|---|
| Ingress | **Traefik v3.7.17**, chart `traefik` 42.3.1 (MIT licence), standard `Ingress` objects | ingress-nginx is retired. Traefik is maintained, serves plain `Ingress` (and Gateway API later), and passes WebSockets through. It listens on the kind node's host ports: 80 (prod), 8090 (staging), 3000 (Grafana), 9090 (Prometheus). |
| Data stores | **Minimal in-repo StatefulSets** (`charts/circuitbreaker/charts/{redpanda,cassandra,postgres}`) with **the same images as Docker Compose**: `redpandadata/redpanda:v26.2.6` (Redpanda Community Edition, Business Source Licence, free to use), `cassandra:5.0.9` and `postgres:16.15` (Docker official images; Apache 2.0 and PostgreSQL licences) | No Bitnami. The vendor charts we could use instead bring operators, TLS and sidecars a single laptop node does not need. Our own ~100-line templates run exactly what Compose runs, and their init Jobs run the same `dev/` scripts. |
| Metrics and dashboards | **kube-prometheus-stack 92.22.0** (Prometheus Operator v0.94.2, Prometheus 3.16, Grafana 13), Apache 2.0, trimmed: no Alertmanager, node-exporter, control-plane scrapes or default rules | The standard way to get Prometheus, Grafana, kube-state-metrics and the `ServiceMonitor`/`PrometheusRule` objects our charts use. The SRE agent polls Prometheus for alerts (no Alertmanager needed). |
| Logs | **Loki 3.7.9**, chart `grafana-community/loki` 20.4.1, one process (Monolithic) on its own disk, no caches or gateway | The supported open-source chart. Monolithic mode handles tens of GB a day, far more than we produce. |
| Log collector | **Grafana Alloy v1.21.3**, chart `grafana/alloy` 1.15.0, reading `/var/log/pods` on the node | Promtail's successor (ADR-0008). Reading files, not tailing through the Kubernetes API: tailing doubled the API server's memory (measured). |
| Images | A **local registry** container (`registry:3.0.0`, `localhost:5001`) on kind's network; images tagged by git SHA | Like a real registry: an unchanged service keeps its tag, so it is neither rebuilt nor rolled. Phase 6 pushes to GHCR instead. |
| Network policies | **kindnet** (kind's own CNI, v20260820) | It enforces `NetworkPolicy`; we tested it (a deny-all policy blocks a connection) before relying on it, so no extra CNI and no extra memory. |
| Chart downloads | Pulled once into `build/k8s/charts/`, by OCI (`ghcr.io`) where published, Alloy by its release URL **checked against the repository index's sha256** | The HTTPS repository indexes are many megabytes and timed out on a slow connection; demo day must not depend on them. |

The three platform values files live in `infra/platform/` so Phase 6's Terraform installs the same thing.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| ingress-nginx | Retired in March 2026: no security fixes. |
| Gateway API (e.g. Envoy Gateway) instead of `Ingress` | The future direction, but a second set of objects to learn for no gain on one laptop; Traefik can serve Gateway API later without a new controller. |
| Bitnami Redpanda/Cassandra/PostgreSQL charts | Free images moved to an unmaintained legacy repository (August 2025). |
| Redpanda's official chart | Apache 2.0 and maintained, but defaults to TLS, cert-manager and sidecars; more to configure away than our template is long. |
| K8ssandra / CloudNativePG operators | Production-grade, but each is one more controller (and its memory) for one single-node database. |
| Prometheus + Grafana as separate charts | kube-prometheus-stack gives the operator, kube-state-metrics and Grafana wiring in one pinned release. |
| Promtail | Deprecated (ADR-0008). |

## Consequences

- ✅ Every component is maintained and free to use, and pinned to an exact version.
- ✅ Compose and Kubernetes run the same database images and the same init scripts (`dev/`, linked into the
  charts), so there is one source for topics, keyspaces and roles.
- ✅ Demo day needs no chart repository: the packages are cached locally.
- ❌ Our data-store templates are ours to maintain (upgrades, probes); they are deliberately minimal and single-node.
- ❌ Traefik-specific annotations (`router.entrypoints`) tie the Ingress objects to Traefik.

## Viva Notes

**Q: Why Traefik and not the NGINX ingress controller everyone uses?**
A: The community ingress-nginx controller was retired in March 2026 and gets no security fixes. Traefik is
maintained, serves the same standard `Ingress` objects, and passes WebSockets through.

**Q: Why did you write your own StatefulSets for Cassandra and Postgres instead of using a chart?**
A: The common charts were Bitnami's, whose free images were withdrawn in 2025. The vendor alternatives bring
operators and TLS that a single laptop node does not need. A minimal StatefulSet running the same image as Docker
Compose is easier to explain and to trust.

**Q: How do you know the network policies actually do anything?**
A: We tested kind's CNI with a deny-all policy before relying on it, and `make k8s-check c=netpol` proves that
forbidden connections (a service reaching another service's database, a bot going around the gateway) fail.

**Q: Why cache the charts locally?**
A: The repository indexes are large and timed out on a slow connection. Demo day may be on a phone hotspot, so the
pinned packages are downloaded once (and Alloy's is checked against its published hash).
