# 0007 — Kubernetes (kind) with a Helm Umbrella Chart; Staging and Prod Namespaces

- **Status:** Accepted — CD part amended by [ADR-0014](0014-gitops-with-argo-cd.md) (push-based `helm upgrade`
  replaced by GitOps with Argo CD); the cluster and namespace parts are created by Terraform per
  [ADR-0015](0015-terraform-local-platform.md)
- **Date:** 2026-11-02
- **Deciders:** Mohammad Oweis (owner), Kaizer Dewaswala, MD Asif Ali

## Context

We need a real orchestrator for rolling deploys, rollbacks, scaling, restarts, health probes and RBAC — they are both
syllabus topics and the SRE agent's tools. We have no cloud budget. We also need staging and prod environments and a
way to deploy 8 services + data stores consistently.

## Decision

- **Docker Compose** for fast local development.
- **kind** (Kubernetes in Docker) for a real Kubernetes cluster on a laptop and inside CI runners.
- **Two namespaces**, `staging` and `prod`, each with its own data stores, secrets and settings; plus `observability`.
- **One Helm umbrella chart** (`charts/circuitbreaker`) with one shared library sub-chart used by all services, and
  `values-staging.yaml` / `values-prod.yaml`.
- **Push-based CD** from GitHub Actions (`helm upgrade`), promoting the same image tag from staging to prod.
  *(Superseded by [ADR-0014](0014-gitops-with-argo-cd.md): Argo CD now pulls each service's release from Git; the
  umbrella chart's shared service sub-chart is what Argo CD renders.)*
- **No HorizontalPodAutoscaler** — the SRE agent is the scaler (max 5 replicas).

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| **Managed Kubernetes (EKS/GKE/AKS)** | Costs money; kind gives the same Kubernetes API for learning. |
| **minikube / k3d** | Both fine; kind is the standard for CI (official GitHub Action) and multi-node clusters. |
| **Docker Compose only** | No rollouts/rollback history, no RBAC, no probes-driven restarts — the agent would have nothing real to act on. |
| **Two separate clusters for staging/prod** | Stronger isolation, but too heavy for a laptop. |
| **Kustomize instead of Helm** | Good for overlays, but Helm's packaging, release history and umbrella charts suit deploying the whole system as one versioned release. |
| **GitOps (Argo CD / Flux)** | *Originally rejected* as an extra component. **Later adopted in [ADR-0014](0014-gitops-with-argo-cd.md)**: the textbook's infrastructure includes Argo CD as its GitOps deployment server (Mitra & Nadareishvili, Ch. 7), and pull-based deployment turned out to *solve* the laptop-reachability problem rather than add to it. |

## Consequences

- ✅ Real rollouts, revisions, rollbacks, scaling, probes and RBAC for the agent and the demo.
- ✅ Same chart and images for staging and prod → "build once, deploy many".
- ❌ Namespaces in one cluster don't isolate cluster-wide failures (a node crash affects both).
- ❌ Laptop resources are tight (two copies of Cassandra/Redpanda/Postgres) — we use small resource settings.
- ❌ Push-based CD needs cluster credentials in CI (here, an ephemeral kind cluster inside the runner). *(Removed by
  ADR-0014: CI no longer deploys.)*

## Viva Notes

**Q: Why kind and not a cloud cluster?**
A: Zero cost and the same Kubernetes API. Everything we do (Helm, rollouts, RBAC) would work on EKS with only
endpoint and credential changes.

**Q: What does the umbrella chart give you?**
A: One versioned package of every service's chart and the data stores, with shared defaults and per-environment
values. Since ADR-0014, Argo CD deploys each service from it as its own Application, so services still release
independently; `helm install` of the whole umbrella is used for quick local and CI test installs.

**Q: Why no autoscaler?**
A: Two controllers fighting over replica counts would be chaotic. For the demo, the agent decides, within a hard limit
of 5 replicas.
