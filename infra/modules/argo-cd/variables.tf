variable "repo_root" {
  description = "The repository's root folder (for infra/platform/argo-cd.yaml)."
  type        = string
}

variable "chart_cache" {
  description = "Folder with the downloaded chart packages (build/k8s/charts)."
  type        = string
}

variable "repo_url" {
  description = "The Git repository Argo CD pulls from (SSH form, for the deploy key)."
  type        = string
}

variable "repo_ssh_private_key" {
  description = "Argo CD's read-only deploy key (private half) for repo_url."
  type        = string
  sensitive   = true
}

variable "target_revision" {
  description = "The branch Argo CD follows: main, or a pull request's branch while it is tested."
  type        = string
}

variable "environments" {
  description = "The environments Argo CD manages (the others are left to make k8s-up while developing)."
  type        = list(string)
}
