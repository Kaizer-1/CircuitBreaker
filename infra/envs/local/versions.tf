terraform {
  required_version = "~> 1.17.0"

  # Pinned exactly; .terraform.lock.hcl (committed) pins their checksums too.
  required_providers {
    # Community provider (not HashiCorp's), hence the exact pin (ADR-0015).
    kind = {
      source  = "tehcyx/kind"
      version = "0.13.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "3.5.0"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "3.3.0"
    }
  }

  # Local state (ADR-0015): terraform.tfstate in this folder, git-ignored.
  backend "local" {}
}
