# The Sealed Secrets controller (ADR-0019, amending ADR-0015's "Secrets from Terraform variables"): Secrets live in
# Git encrypted to this controller's public key, and only the controller, with its private key, can decrypt them.

terraform {
  required_providers {
    helm = {
      source = "hashicorp/helm"
    }
  }
}

# A rebuilt cluster must decrypt the SealedSecrets already in Git, so the sealing key from the backup in private/
# goes in BEFORE the controller starts (with an active key present it does not make a new one). Applied by
# scripts/sealed_secrets.py with kubectl, so the private key never enters Terraform's state. No backup (the very
# first cluster): nothing is restored, the controller makes a key, and a person backs it up (make sealing-key-backup).
resource "terraform_data" "restore_key" {
  triggers_replace = [var.cluster_ready]

  provisioner "local-exec" {
    command = "python3 ${var.repo_root}/scripts/sealed_secrets.py restore"
  }
}

resource "helm_release" "controller" {
  name       = "sealed-secrets"
  chart      = "${var.chart_cache}/sealed-secrets-2.22.0.tgz"
  namespace  = "kube-system"
  values     = [file("${var.repo_root}/infra/platform/sealed-secrets.yaml")]
  wait       = true
  depends_on = [terraform_data.restore_key]
}
