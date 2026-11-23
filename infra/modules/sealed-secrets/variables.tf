variable "repo_root" {
  description = "The repository's root folder (for scripts/ and infra/platform/)."
  type        = string
}

variable "chart_cache" {
  description = "Folder with the downloaded chart packages (build/k8s/charts)."
  type        = string
}

variable "cluster_ready" {
  description = "Changes when the cluster is recreated (the kind-cluster module's `ready`): restore the key again."
  type        = string
}
