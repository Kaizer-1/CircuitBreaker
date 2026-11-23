variable "config_file" {
  description = "Path of the kind configuration (infra/kind/cluster.yaml)."
  type        = string
}

variable "repo_root" {
  description = "The repository's root folder (for scripts/k8s.py)."
  type        = string
}
