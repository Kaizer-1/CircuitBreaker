output "name" {
  value = kind_cluster.this.name
}

output "context" {
  description = "The kubectl context kind writes into ~/.kube/config."
  value       = "kind-${kind_cluster.this.name}"
}

output "endpoint" {
  value = kind_cluster.this.endpoint
}

output "cluster_ca_certificate" {
  value = kind_cluster.this.cluster_ca_certificate
}

output "client_certificate" {
  value = kind_cluster.this.client_certificate
}

output "client_key" {
  value     = kind_cluster.this.client_key
  sensitive = true
}

output "ready" {
  description = "Depend on this to run after the registry is connected."
  value       = terraform_data.registry.id
}
