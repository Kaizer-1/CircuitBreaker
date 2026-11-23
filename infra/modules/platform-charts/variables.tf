variable "repo_root" {
  description = "The repository's root folder (for infra/platform/*.yaml)."
  type        = string
}

variable "chart_cache" {
  description = "Folder with the downloaded chart packages (build/k8s/charts, filled by scripts/k8s.py charts)."
  type        = string
}

variable "grafana_admin_password" {
  description = "Grafana's admin password."
  type        = string
  sensitive   = true
}
