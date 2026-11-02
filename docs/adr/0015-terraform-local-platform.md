# 0015 — Terraform for the Local Platform; LocalStack Optional

- **Status:** Accepted — amends [ADR-0010](0010-terraform-localstack.md) (Terraform stays; its main target moves
  from LocalStack to the local Kubernetes platform)
- **Date:** 2026-11-03
- **Deciders:** Mohammad Oweis (owner, Cloud Platform Team), Kaizer Dewaswala, MD Asif Ali

## Context

ADR-0010 pointed Terraform mainly at LocalStack (emulated AWS: Secrets Manager, SSM, S3, IAM). The kind cluster
itself was made by a script.

Having now read the textbook's infrastructure chapter (Mitra & Nadareishvili, Ch. 7), we can compare our setup with
its approach. The book uses Terraform **modules** to build three components:

1. a **network** (an AWS VPC with four subnets, routing and NAT);
2. a **managed Kubernetes cluster** (EKS);
3. a **GitOps deployment server** (Argo CD), installed into the cluster with Terraform's **Kubernetes and Helm
   providers**.

A **sandbox environment** file then calls those modules. The book keeps one module per Git repository and applies
changes through a GitHub Actions pipeline (Ch. 6–7).

Under ADR-0010, our Terraform did not create the cluster or the deployment server. Those are the parts that
matter most for running our services.

## Decision

**Terraform is the primary IaC tool for the local platform**:

```text
infra/
├── modules/
│   ├── kind-cluster/   # kind provider: the 'circuitbreaker' cluster (control plane + 2 workers)
│   ├── environment/    # kubernetes provider: one namespace with its ResourceQuota, the sre-agent Role/RoleBinding, Secrets
│   ├── argo-cd/        # helm provider: the argo-cd chart into namespace 'argocd', plus the ApplicationSet (ADR-0014)
│   └── localstack/     # OPTIONAL: aws provider pointed at LocalStack (S3 event-archive bucket)
└── envs/
    └── local/main.tf   # the textbook's "sandbox environment": calls kind-cluster, environment × {staging, prod}, argo-cd
```

- **kind provider:** a community Terraform provider for kind (for example `tehcyx/kind`). We pin its exact version
  when Phase 5 starts.
- **Secrets:** Terraform creates the Kubernetes Secrets (`OPENROUTER_API_KEY`, JWT/bot secrets, admin passcode, DB
  passwords). Their values come from `TF_VAR_*` environment variables or a git-ignored `secrets.auto.tfvars`, never
  from Git.
- **Settings that are not secret:** these are Helm values in `deploy/envs/<env>/`, for example the agent's approval
  default and `SRE_AGENT_MODEL`.
- **State:** a local backend, so the state file stays on the laptop and is git-ignored. Terraform state contains
  secret values in plain text, so it must never be committed.
- **Pipeline:** CI runs `terraform fmt -check` and `validate` on every PR. It also creates the whole local platform
  inside a runner (`terraform apply` on `envs/local`), checks that Argo CD's pods are running, and runs
  `terraform destroy`. These are the textbook's "test the environment" and "clean up" steps. On the demo laptop, the
  Cloud Platform Team runs `make infra` (plan, then apply).
- **LocalStack becomes optional:** the `localstack` module adds AWS-style extras, such as the nightly event-archive
  bucket. Nothing in the core system depends on it.
  ⚠️ **Check LocalStack's current licence and free-tier terms before using it.** They have changed over time.

### Textbook component → our local equivalent

| Textbook (Ch. 7, real AWS) | Our local equivalent | Notes |
|---|---|---|
| VPC + 4 subnets, routing, NAT gateways (`module-aws-network`) | The kind cluster's Docker network, plus Kubernetes namespaces and NetworkPolicies | We don't need to configure routing or NAT, because kind's Docker network gives every node a network address. Isolation between workloads comes from NetworkPolicies. |
| EKS managed Kubernetes (`module-aws-kubernetes`) | kind cluster (`modules/kind-cluster`) | Same Kubernetes API, zero cost. The control plane is a container, not a managed service. |
| Argo CD via the Helm provider (`module-argo-cd`) | Argo CD via the Helm provider (`modules/argo-cd`) | Same approach; see ADR-0014. |
| Terraform modules called from a sandbox environment | Terraform modules called from `infra/envs/local` | Same module pattern. Our modules are folders in the monorepo rather than one Git repository each. |
| GitHub Actions infrastructure pipeline (Ch. 6) | GitHub Actions: fmt/validate on PRs, and a create–check–destroy test of `envs/local` in a runner | The laptop cluster can't be reached from GitHub, so the real apply runs locally with `make infra`. |
| AWS IAM roles for the cluster and nodes | Kubernetes RBAC (e.g. the `sre-agent` Role) | Least privilege inside the cluster. |

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| **Keep ADR-0010 as it was** (Terraform mainly for LocalStack) | Terraform would manage emulated cloud extras while the parts that matter (cluster, namespaces, Argo CD) stayed outside IaC. It also doesn't match the textbook's module structure. |
| **Real AWS (EKS) as in the textbook** | Costs money and risks surprise bills. kind gives the same Kubernetes API. |
| **Scripts (`kind create cluster`, `helm install argo-cd`)** | Not declarative: no plan/diff and no state. Running a script twice is not guaranteed to be safe. |
| **One Git repository per module** (as in the textbook) | Good for sharing modules between many teams. For one platform owner, folders in the monorepo are simpler and are reviewed in the same PR as the docs. |
| **Remote state in LocalStack S3 with locking** | Only needed when several people apply at once. One platform owner applies; local state is simpler. |

## Consequences

- ✅ The whole platform (cluster, namespaces, RBAC, secrets, Argo CD) comes from one `terraform apply`. It can be
  destroyed and rebuilt identically.
- ✅ It maps one-to-one onto the textbook's three infrastructure components, so we can explain each difference in
  the viva.
- ✅ No dependency on an emulator's licence for anything the demo needs.
- ❌ The kind provider is maintained by the community, not by HashiCorp, so we pin its version.
- ❌ Local state has no locking and no shared copy. This is fine for one platform owner; a team would move it to
  shared storage.
- ❌ Fewer "cloud" resources to show. The optional LocalStack module can still demonstrate them.

## Viva Notes

**Q: What does your Terraform create?**
A: The kind cluster, the `staging` and `prod` namespaces with quotas, the agent's RBAC and the Secrets, and Argo CD.
Optionally it also creates an S3-style archive bucket in LocalStack.

**Q: How does your setup map onto the textbook's AWS infrastructure?**
A: VPC and subnets map to kind's Docker network plus namespaces and network policies; EKS maps to kind; Argo CD is the
same tool installed the same way, through the Helm provider. Like the book, we call reusable modules from one
environment file.

**Q: Why is LocalStack optional now?**
A: Nothing the system needs depends on emulated AWS any more, and its licence terms change. Keeping it optional
removes a fragile dependency.

**Q: How would you move to real AWS?**
A: Swap the `kind-cluster` module for network and EKS modules like the textbook's. The `environment` and `argo-cd`
modules, the charts and the GitOps flow stay the same.
