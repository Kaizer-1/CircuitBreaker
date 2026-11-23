# The kind cluster (ADR-0015): one node, built from infra/kind/cluster.yaml so that file stays the only place its
# settings live (node image by digest, host ports for the ingress, Grafana and Prometheus).

terraform {
  required_providers {
    kind = {
      source = "tehcyx/kind"
    }
  }
}

locals {
  config = yamldecode(file(var.config_file))
}

resource "kind_cluster" "this" {
  name           = local.config.name
  wait_for_ready = true
  # The provider also writes its own kubeconfig, holding the cluster's client key, as infra/envs/local/
  # circuitbreaker-config (git-ignored); kind adds the context kind-circuitbreaker to ~/.kube/config, which make and
  # kubectl use. (Moving that file later would make Terraform replace the cluster.)

  kind_config {
    kind        = local.config.kind
    api_version = local.config.apiVersion

    dynamic "node" {
      for_each = local.config.nodes
      content {
        role   = node.value.role
        image  = node.value.image
        labels = try(node.value.labels, null)

        dynamic "extra_port_mappings" {
          for_each = try(node.value.extraPortMappings, [])
          content {
            container_port = extra_port_mappings.value.containerPort
            host_port      = extra_port_mappings.value.hostPort
            listen_address = extra_port_mappings.value.listenAddress
            protocol       = extra_port_mappings.value.protocol
          }
        }
      }
    }
  }
}

# The local image registry (localhost:5001) joins kind's network and the node learns where it is. The registry is a
# plain container outside the cluster (ADR-0018), so this calls the same code make k8s-up uses; it runs again
# whenever the cluster is recreated.
resource "terraform_data" "registry" {
  triggers_replace = [kind_cluster.this.id]

  provisioner "local-exec" {
    command = "python3 ${var.repo_root}/scripts/k8s.py registry"
  }
}
