# The local platform (ADR-0015): the textbook's "sandbox environment" (Mitra & Nadareishvili, Ch. 7), one file that
# calls the modules. `make platform` plans and applies it; `make k8s-up` applies it without Argo CD when there is no
# cluster yet (developing with local images).
#
#   kind-cluster      the cluster (infra/kind/cluster.yaml) and the local registry's connection
#   environment ×2    namespaces staging and prod: ResourceQuota, default-deny policy, the SRE agent's Role
#   platform-charts   Traefik, Prometheus + Grafana, Loki, Alloy (ADR-0018)
#   sealed-secrets    the controller that decrypts the SealedSecrets in Git (ADR-0019)
#   argo-cd           Argo CD + the ApplicationSets (ADR-0014), only for the environments in var.gitops_environments
#
# State is local (terraform.tfstate here, git-ignored): it holds secret values (Argo CD's deploy key, Grafana's
# password), so it never goes to Git.

locals {
  repo_root   = abspath("${path.root}/../../..")
  chart_cache = "${local.repo_root}/build/k8s/charts"
  deploy_key  = coalesce(var.repo_ssh_private_key_file, "${local.repo_root}/private/keys/argocd-deploy-key")
}

module "kind_cluster" {
  source      = "../../modules/kind-cluster"
  config_file = "${local.repo_root}/infra/kind/cluster.yaml"
  repo_root   = local.repo_root
}

provider "kubernetes" {
  host                   = module.kind_cluster.endpoint
  cluster_ca_certificate = module.kind_cluster.cluster_ca_certificate
  client_certificate     = module.kind_cluster.client_certificate
  client_key             = module.kind_cluster.client_key
}

provider "helm" {
  kubernetes = {
    host                   = module.kind_cluster.endpoint
    cluster_ca_certificate = module.kind_cluster.cluster_ca_certificate
    client_certificate     = module.kind_cluster.client_certificate
    client_key             = module.kind_cluster.client_key
  }
}

module "environment" {
  source     = "../../modules/environment"
  for_each   = toset(["staging", "prod"])
  name       = each.key
  depends_on = [module.kind_cluster]
}

module "platform_charts" {
  source                 = "../../modules/platform-charts"
  repo_root              = local.repo_root
  chart_cache            = local.chart_cache
  grafana_admin_password = var.grafana_admin_password
  depends_on             = [module.kind_cluster]
}

module "sealed_secrets" {
  source        = "../../modules/sealed-secrets"
  repo_root     = local.repo_root
  chart_cache   = local.chart_cache
  cluster_ready = module.kind_cluster.ready
}

module "argo_cd" {
  source               = "../../modules/argo-cd"
  count                = length(var.gitops_environments) > 0 ? 1 : 0
  repo_root            = local.repo_root
  chart_cache          = local.chart_cache
  repo_url             = var.repo_url
  repo_ssh_private_key = length(var.gitops_environments) > 0 ? file(pathexpand(local.deploy_key)) : ""
  target_revision      = var.target_revision
  environments         = var.gitops_environments
  # Argo CD deploys into namespaces that already have their quota and policies, with the SealedSecret controller
  # and the Prometheus Operator's CRDs (our ServiceMonitors) in place.
  depends_on = [module.environment, module.platform_charts, module.sealed_secrets]
}
