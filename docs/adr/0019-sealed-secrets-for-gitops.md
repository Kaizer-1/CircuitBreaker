# 0019 — Sealed Secrets: Encrypted Secrets in Git for GitOps

- **Status:** Accepted. Amends [ADR-0015](0015-terraform-local-platform.md): its "Terraform creates the Kubernetes
  Secrets from `TF_VAR_*` variables, never from Git" now applies to the **platform's** two bootstrap secrets only
  (below); every secret the services use is a **SealedSecret in Git**. **An exception to the design freeze**
  ([roadmap](../07-roadmap.md#design-freeze)), made by the owner on 2026-11-23: it adds an infrastructure component
  (the Sealed Secrets controller). Reason recorded with it: besides GitOps, it serves the owner's Kubernetes-security
  course (secure configuration and secret management).
- **Date:** 2026-11-23
- **Deciders:** Kaizer Dewaswala (owner), Mohammad Oweis (Cloud Platform Team), MD Asif Ali

## Context

With GitOps ([ADR-0014](0014-gitops-with-argo-cd.md)), Git is the source of truth for what runs, and Argo CD makes
the cluster match it. Secrets do not fit that picture by themselves:

- A Kubernetes `Secret` is **not encrypted**. Its values are only base64-encoded, which anyone can decode, and by
  default the API server stores them unencrypted in etcd
  ([Kubernetes documentation, "Secrets"](https://kubernetes.io/docs/concepts/configuration/secret/)). A Secret
  committed to Git would publish the JWT signing key, the admin passcode and every database password to everyone
  who can read the repository, forever (Git keeps history).
- ADR-0015 kept them out of Git: Terraform creates them from variables. That works, but then part of what runs is
  not in Git. A rebuilt environment needs whoever holds the variables, and a rotated secret is invisible in the
  history.

> **base64** is an *encoding*, not encryption: a reversible way to write bytes as text, with no key. `echo
> Y2Itc2VjcmV0 | base64 -d` prints `cb-secret`.
> **Sealed Secrets** (Bitnami, Apache 2.0; [project](https://github.com/bitnami/sealed-secrets)) has two parts: a
> **controller** in the cluster holding a private key, and **kubeseal**, a command-line tool that encrypts a Secret
> with the controller's **public** key into a `SealedSecret` object. Only the controller can decrypt it, and it then
> creates the ordinary `Secret` the pods read.

## Decision

**Every secret a service uses is a SealedSecret committed in Git**, one file per secret per environment in
`deploy/envs/<env>/sealed/`, synced by Argo CD like everything else (Applications `secrets-staging`,
`secrets-prod`).

| Part | Choice | Why |
|---|---|---|
| Controller | Chart `sealed-secrets` 2.22.0, controller **0.41.2 pinned by version and image digest**, in `kube-system`, installed by Terraform's `sealed-secrets` module | Bitnami keeps only the newest release under this image name (older ones move to `bitnamilegacy`), so a tag alone could later point elsewhere or vanish. The digest is also what `make k8s-prepull` pulls. |
| Scope | `strict` (the default) | A SealedSecret decrypts only under its own name in its own namespace. A staging secret copied into prod, or a renamed one, is refused (tested). |
| Sealing | `make seal env=<env>` (`scripts/sealed_secrets.py`) with the **public certificate** committed in `infra/sealed-secrets/sealing-cert.pem` | Sealing needs no cluster access and no secret. Only the plaintext source (below) is secret. |
| Plaintext source | `private/secrets/<env>.json`, git-ignored, mode 600; missing values are generated at random (32 random bytes; the admin passcode 96 random bits in four short groups) | The values must exist somewhere to re-seal after a key loss or to rotate one secret. Only changed values are re-sealed, so Git shows exactly what was rotated. |
| Key renewal | **Off** (`keyrenewperiod: "0"`; the controller's default is a new key every 30 days) | Each new key would need a new backup; a forgotten one would leave secrets sealed with a key that exists nowhere else. We rotate the key on purpose ([deployment doc §5](../03-architecture/07-deployment-and-environments.md#5-infrastructure-as-code-terraform)). |
| Key backup | `make sealing-key-backup` (a person runs it) writes the private key to `private/sealed-secrets/sealing-key.json`; a copy is kept **off the laptop** | Without the key, no new cluster can decrypt what is in Git. |
| Key restore | `make platform` puts the backed-up key in **before** the controller starts (with an active key present it makes none), applied with `kubectl`, so the private key never enters Terraform's state | A rebuilt cluster decrypts the same SealedSecrets. Tested by a drill: `make k8s-check c=sealed-drill`. |

**What stays outside Git (Terraform variables, git-ignored `secrets.auto.tfvars`):** the two secrets that must
exist **before** Argo CD can read Git:

- **Argo CD's repository credential** (a read-only deploy key). This is the *bootstrap* problem: Argo CD needs it to
  read Git, and the SealedSecrets reach the cluster *through* Argo CD, so it cannot itself be one of them.
- **Grafana's admin password**: Grafana is installed by Terraform with the platform, before Argo CD.

Both are therefore in Terraform's state, one of the reasons the state file is git-ignored and stays on the laptop.

**Placeholders for Phase 7:** `openrouter-api-key` and `sre-agent-github-app` are sealed now with placeholder
values, so the charts' references resolve; Phase 7 re-seals them with real values (`make seal env=<env>
rotate="openrouter-api-key"` after editing the plaintext source).

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| **Keep ADR-0015** (Terraform creates the Secrets from variables) | Works, but part of what runs is outside Git: no history of a rotation, and a rebuild needs whoever holds the variables. Kept only for the two bootstrap secrets. |
| **SOPS** (Mozilla's tool, now CNCF) with age or PGP keys | Encrypts the values inside an ordinary YAML file; good, and widely used. But Argo CD cannot read it without a plugin (a custom repo-server image or a config-management plugin), and the decryption key must then live in Argo CD. Sealed Secrets needs no Argo CD change: a SealedSecret is just another Kubernetes object. |
| **External Secrets Operator** with a secret manager (Vault, AWS Secrets Manager) | The production answer in a cloud: Git holds only references. It needs a secret store running somewhere; Vault on the laptop is one more stateful service, and AWS costs money (LocalStack's terms change, ADR-0015). |
| **Plain Secrets in Git** | base64 is not encryption: it would publish every credential. |
| **Encryption at rest in etcd** (`EncryptionConfiguration`) | Protects the cluster's database, not Git. Worth adding on a real cluster; it solves a different problem. |

## Consequences

- ✅ Git holds the whole desired state, secrets included; `git log deploy/envs/prod/sealed/` shows every rotation.
- ✅ A public repository could hold these files safely: without the controller's private key they are noise.
- ✅ Strict scope: a secret cannot be moved to another environment or renamed to be read by another service.
- ✅ No change to Argo CD and no extra running service besides one small controller (about 30 MiB).
- ❌ **The private key is now the most valuable file.** Lost without a backup: every SealedSecret in Git is
  unreadable to a new cluster (running Secrets keep working until the cluster is rebuilt). Recovery is re-sealing
  everything from the plaintext source with the new key's certificate; if the plaintext source is lost too, every
  credential must be rotated. Leaked: an attacker with the repository can decrypt everything; rotate the key **and**
  every secret.
- ❌ Two copies of each value exist outside the cluster: the plaintext source in `private/` and the key backup.
  Both are git-ignored and must be kept off shared machines.
- ❌ Rotating a value does not restart the pods that read it (environment variables are read at start): after the
  sync, restart them (`kubectl rollout restart`, which Argo CD ignores).
- ✅ *Added 2026-11-25:* the Cassandra superuser's password is random and sealed like every other value. Until then
  it was the image's default (`cassandra`/`cassandra`), sealed but known. The init job gives a cluster that still has
  the default the Secret's password, once (`ALTER ROLE`), and Cassandra's readiness probe needs no password (it sends
  a CQL `OPTIONS` request, allowed before login), so the Cassandra pod holds none and changing it never makes the
  database unready. `make k8s-check c=sealed` proves the default is refused. A later change from one random value to
  another follows the rotation steps: `ALTER ROLE` first, then re-seal.

## Viva Notes

**Q: Why Sealed Secrets for GitOps?**
A: GitOps means Git holds everything that runs, but a Secret in Git would publish the credentials. A SealedSecret is
encrypted with the cluster controller's public key, so it is safe in Git, and only that controller can turn it back
into a Secret. Argo CD syncs it like any other object.

**Q: Isn't a Kubernetes Secret already secret?**
A: No. Its values are base64, an encoding anyone can reverse without a key, and by default they are stored
unencrypted in etcd. Secrets protect by access control (RBAC), not by encryption.

**Q: What if the controller's key is lost?**
A: Running Secrets keep working, but no new cluster can decrypt what is in Git. With our backup, `make platform`
restores the key before the controller starts; we test that with a drill. Without the backup, we re-seal every secret
from the plaintext source with the new key; without that too, every credential has to be changed.

**Q: Why can't Argo CD's own Git credential be a SealedSecret?**
A: Argo CD needs it to read Git, and the SealedSecrets come from Git through Argo CD: a chicken-and-egg problem. It
is created by Terraform from a git-ignored variable, so it lives in Terraform's state, which is why that state is
never committed.

**Q: How was the Cassandra superuser's password changed on a running cluster without taking the database down?**
A: In two parts. Cassandra's readiness probe stopped logging in at all: it asks the server which protocol options it
supports, which needs no password, so the database pod no longer depends on the password. The init job (re-run by
Argo CD because its script changed) waits until the Secret holds the new sealed value, logs in with the image's
default once, runs `ALTER ROLE`, and from then on uses the new password. Re-running it changes nothing.

**Q: Why not SOPS or Vault?**
A: SOPS needs a plugin in Argo CD and the decryption key inside it; Vault (with External Secrets) is a production
answer but one more stateful service on a laptop. Sealed Secrets needs neither.
