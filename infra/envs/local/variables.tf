# Values that are not secret have defaults here. The secret ones come from a git-ignored secrets.auto.tfvars (copy
# secrets.auto.tfvars.example) or TF_VAR_* variables, never from Git.

variable "gitops_environments" {
  description = "Environments Argo CD deploys from Git. [] = no Argo CD (make k8s-up on a fresh laptop)."
  type        = list(string)
  default     = ["staging", "prod"]
  validation {
    condition     = alltrue([for e in var.gitops_environments : contains(["staging", "prod"], e)])
    error_message = "The environments are staging and prod."
  }
}

variable "profile" {
  description = <<-EOT
    full = the demo platform (both environments, monitoring, Sealed Secrets). ci = CI's slim check on a GitHub runner:
    staging only, no monitoring, no Sealed Secrets controller and no ingress, the
    smaller requests of deploy/profiles/ci.yaml, Argo CD reading Git over HTTPS with repo_https_token.
  EOT
  type        = string
  default     = "full"
  validation {
    condition     = contains(["full", "ci"], var.profile)
    error_message = "The profiles are full and ci."
  }
}

variable "repo_https_token" {
  description = "A token for Argo CD to read Git over HTTPS instead of the deploy key (CI: the job's GITHUB_TOKEN)."
  type        = string
  sensitive   = true
  default     = null
}

variable "repo_url" {
  description = "The Git repository Argo CD pulls from."
  type        = string
  default     = "git@github.com:Kaizer-1/CircuitBreaker.git"
}

variable "target_revision" {
  description = "The branch Argo CD follows (a pull request's branch while it is being tested)."
  type        = string
  default     = "main"
}

variable "repo_ssh_private_key_file" {
  description = "Argo CD's read-only deploy key (private half). null = private/keys/argocd-deploy-key (git-ignored)."
  type        = string
  default     = null
}

variable "grafana_admin_password" {
  description = "Grafana's admin password."
  type        = string
  sensitive   = true
  default     = "cb-dev-grafana" # the Phase 5 local placeholder; set a real one in secrets.auto.tfvars
}
