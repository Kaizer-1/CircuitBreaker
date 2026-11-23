# The platform's third-party charts (ADR-0018): Traefik (the ingress), kube-prometheus-stack (Prometheus, Grafana),
# Loki and Alloy, with the values in infra/platform/. Installed from the packages scripts/k8s.py downloads once into
# build/k8s/charts (pinned, checked against their published sha256), so a rebuild needs no chart repository.
# Our own dashboards and alert rules (charts/observability) are not here: Argo CD deploys them from Git.

terraform {
  required_providers {
    helm = {
      source = "hashicorp/helm"
    }
    kubernetes = {
      source = "hashicorp/kubernetes"
    }
  }
}

locals {
  values = "${var.repo_root}/infra/platform"
}

resource "kubernetes_namespace_v1" "observability" {
  metadata {
    name = "observability"
  }
}

# Grafana's admin login (anonymous visitors may only look). A platform secret, like Argo CD's repository key: it
# exists before Argo CD does, so it comes from a Terraform variable (git-ignored tfvars), not from Git.
resource "kubernetes_secret_v1" "grafana_admin" {
  metadata {
    name      = "grafana-admin"
    namespace = kubernetes_namespace_v1.observability.metadata[0].name
  }
  data = {
    "admin-user"     = "admin"
    "admin-password" = var.grafana_admin_password
  }
}

resource "helm_release" "traefik" {
  name             = "traefik"
  chart            = "${var.chart_cache}/traefik-42.3.1.tgz"
  namespace        = "traefik"
  create_namespace = true
  values           = [file("${local.values}/traefik.yaml")]
  wait             = true
}

resource "helm_release" "kube_prometheus_stack" {
  name       = "kube-prometheus-stack"
  chart      = "${var.chart_cache}/kube-prometheus-stack-92.22.0.tgz"
  namespace  = kubernetes_namespace_v1.observability.metadata[0].name
  values     = [file("${local.values}/kube-prometheus-stack.yaml")]
  wait       = true
  timeout    = 600
  depends_on = [kubernetes_secret_v1.grafana_admin]
}

resource "helm_release" "loki" {
  name      = "loki"
  chart     = "${var.chart_cache}/loki-20.4.1.tgz"
  namespace = kubernetes_namespace_v1.observability.metadata[0].name
  values    = [file("${local.values}/loki.yaml")]
  wait      = true
  timeout   = 600
  # Loki's ServiceMonitor needs the Prometheus Operator's CRDs.
  depends_on = [helm_release.kube_prometheus_stack]
}

resource "helm_release" "alloy" {
  name       = "alloy"
  chart      = "${var.chart_cache}/alloy-1.15.0.tgz"
  namespace  = kubernetes_namespace_v1.observability.metadata[0].name
  values     = [file("${local.values}/alloy.yaml")]
  wait       = true
  depends_on = [helm_release.loki]
}
