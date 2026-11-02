# 0010 — Terraform against LocalStack

- **Status:** Accepted — amended by [ADR-0015](0015-terraform-local-platform.md): Terraform's primary target is now
  the local platform (kind cluster, namespaces, Argo CD); LocalStack is optional
- **Date:** 2026-11-02
- **Deciders:** Mohammad Oweis (owner), Kaizer Dewaswala, MD Asif Ali

## Context

The syllabus requires Infrastructure as Code and an IaC environment/pipeline. We have no cloud budget. We still want
realistic cloud resources (secrets, configuration, object storage, IAM) and per-environment infrastructure.

## Decision

Use **Terraform** with an `environment` module instantiated for `staging` and `prod`, applied against **LocalStack**
(local AWS emulation) and the Kubernetes provider. Resources: Secrets Manager secrets (OpenRouter key, JWT secret, bot
secret, admin passcode, DB passwords), SSM parameters (agent defaults), an S3 event-archive bucket, IAM roles/policies,
Kubernetes namespaces, quotas, the `sre-agent` Role and network policies. Terraform state lives in LocalStack S3 with
DynamoDB locking. Plans are shown in PRs and applied by CI. Details:
[07-deployment-and-environments.md](../03-architecture/07-deployment-and-environments.md#5-infrastructure-as-code-terraform).

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| **Real AWS** | Costs money and risks surprise bills; LocalStack gives the same APIs for learning. |
| **Pulumi** | Code-first IaC is attractive, but Terraform is the industry default and closer to the textbook. |
| **Plain shell scripts / kubectl apply** | Not declarative; no plan/diff, state or drift detection. |
| **Crossplane** | Kubernetes-native IaC; powerful but much more complex to set up. |

## Consequences

- ✅ Repeatable environments from code; plan/review/apply workflow; easy to move to real AWS (change provider endpoint
  and credentials).
- ✅ Secrets never in Git.
- ❌ LocalStack is an emulation: some behaviours differ from AWS, and some services are not in its free tier
  (LocalStack is optional since ADR-0015; ⚠️ check its current licence/free-tier terms before using it).
- ❌ Another tool to learn and run.

**Comparison with the textbook (added 2026-11-03).** The textbook builds a network, EKS and Argo CD as Terraform
modules on real AWS (Mitra & Nadareishvili, Ch. 7). The component-by-component mapping to our local setup, and the
resulting change of scope, are in [ADR-0015](0015-terraform-local-platform.md#textbook-component--our-local-equivalent).

## Viva Notes

**Q: What is Infrastructure as Code and why use it?**
A: Describing infrastructure in versioned files that a tool applies. It makes environments repeatable, reviewable and
recoverable, and removes manual "click-ops" drift.

**Q: Why LocalStack?**
A: It emulates AWS APIs locally, so we practise real Terraform with no cost or cloud account.

**Q: How would you go to real AWS?**
A: Point the AWS provider at real endpoints with real credentials and a remote state bucket; the modules stay the same.
