output "kube_context" {
  description = "kubectl --context for this cluster."
  value       = module.kind_cluster.context
}

output "gitops_environments" {
  value = var.gitops_environments
}
