variable "name" {
  description = "The environment, also its namespace: staging or prod."
  type        = string
  validation {
    condition     = contains(["staging", "prod"], var.name)
    error_message = "The environments are staging and prod."
  }
}

variable "quota" {
  description = "The namespace's ResourceQuota (hard limits)."
  type        = map(string)
  default = {
    "requests.cpu"    = "3"
    "requests.memory" = "5Gi"
    "limits.cpu"      = "24"
    "limits.memory"   = "10Gi"
    "pods"            = "30"
  }
}
