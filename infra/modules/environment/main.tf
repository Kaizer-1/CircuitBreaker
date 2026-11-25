# One environment (ADR-0015): its namespace, a ResourceQuota, the default-deny network policy and the SRE agent's
# Role. What runs inside comes from Git through Argo CD (or make k8s-up while developing); the Secrets come from
# SealedSecrets in Git (ADR-0019).

terraform {
  required_providers {
    kubernetes = {
      source = "hashicorp/kubernetes"
    }
  }
}

resource "kubernetes_namespace_v1" "this" {
  metadata {
    name = var.name
    labels = {
      "app.kubernetes.io/part-of" = "circuitbreaker"
      "circuitbreaker.io/env"     = var.name
    }
  }
}

# A ceiling on what the namespace may claim, so a runaway (a scale far past the agent's limit of 5, a second copy of
# the system) is refused instead of starving the other environment. Sized from the chart: requests 1.87 CPU and
# 3.3 GiB, limits 17.7 CPU and 6.7 GiB for the whole system, plus room for order-service at 5 replicas and one
# extra pod per rolling update. It does not divide the laptop's CPU: limits add up to more cores than the laptop
# has (deployment doc section 3).
resource "kubernetes_resource_quota_v1" "this" {
  metadata {
    name      = "circuitbreaker"
    namespace = kubernetes_namespace_v1.this.metadata[0].name
  }
  spec {
    hard = var.quota
  }
}

# Default deny: no pod in the namespace accepts a connection unless a policy lets it in. Each service's own policy
# (charts/circuitbreaker/charts/service) and each data store's list their callers. It lives here, not in the chart,
# because each Argo CD Application renders the chart for one service only (global.only).
resource "kubernetes_network_policy_v1" "default_deny" {
  metadata {
    name      = "default-deny-ingress"
    namespace = kubernetes_namespace_v1.this.metadata[0].name
    labels = {
      "app.kubernetes.io/part-of" = "circuitbreaker"
    }
  }
  spec {
    pod_selector {}
    policy_types = ["Ingress"]
  }
}

# The SRE agent's least privilege (SRE agent design, "Defence in depth: Kubernetes RBAC"): read and patch
# Deployments and their scale, read ReplicaSets and Pods for history and status, in its own namespace only. Nothing
# else: no deletes, no Secrets, no exec, no StatefulSets. The chart creates the ServiceAccount `sre-agent`.
resource "kubernetes_role_v1" "sre_agent" {
  metadata {
    name      = "sre-agent"
    namespace = kubernetes_namespace_v1.this.metadata[0].name
  }
  rule {
    api_groups = ["apps"]
    resources  = ["deployments"]
    verbs      = ["get", "list", "patch"]
  }
  rule {
    api_groups = ["apps"]
    resources  = ["deployments/scale"]
    verbs      = ["get", "patch"]
  }
  rule {
    api_groups = ["apps"]
    resources  = ["replicasets"]
    verbs      = ["get", "list"]
  }
  rule {
    api_groups = [""]
    resources  = ["pods"]
    verbs      = ["get", "list"]
  }
}

resource "kubernetes_role_binding_v1" "sre_agent" {
  metadata {
    name      = "sre-agent"
    namespace = kubernetes_namespace_v1.this.metadata[0].name
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.sre_agent.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = "sre-agent"
    namespace = kubernetes_namespace_v1.this.metadata[0].name
  }
}

# chaos-service's least privilege (ADR-0020): it may change exactly one object, the order-service Deployment, by
# name: get and patch it (the bad deploy's FAULT_PROFILE and its removal) and its scale (the reset puts the replicas
# back). Every other fault is the target's own hook, over HTTP. No list, no other Deployment, no StatefulSet, no
# Secret: the data stores are out of reach by permission. The chart creates the ServiceAccount `chaos-service`.
resource "kubernetes_role_v1" "chaos_service" {
  metadata {
    name      = "chaos-service"
    namespace = kubernetes_namespace_v1.this.metadata[0].name
  }
  rule {
    api_groups     = ["apps"]
    resources      = ["deployments", "deployments/scale"]
    resource_names = ["order-service"]
    verbs          = ["get", "patch"]
  }
}

resource "kubernetes_role_binding_v1" "chaos_service" {
  metadata {
    name      = "chaos-service"
    namespace = kubernetes_namespace_v1.this.metadata[0].name
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.chaos_service.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = "chaos-service"
    namespace = kubernetes_namespace_v1.this.metadata[0].name
  }
}
