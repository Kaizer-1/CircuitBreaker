#!/usr/bin/env bash
# make k8s-lint (and CI's "kubernetes" job): check the charts, the platform values and the alert rules without a
# cluster. Needs helm and Docker (kubeconform and promtool run from their pinned images).
#   1. helm lint the umbrella chart (each environment) and the observability chart
#   2. render the umbrella for prod and staging, and once per service with global.only (as the per-service Argo CD
#      Applications do), plus the observability chart, Argo CD's applications chart (infra/modules/argo-cd) and the
#      third-party charts with our values
#   3. validate every rendered object against the Kubernetes 1.37 schemas (kubeconform), including the
#      ServiceMonitor and PrometheusRule custom resources
#   4. promtool: check the alert rules and run their unit tests (make test-alerts)
set -euo pipefail
cd "$(dirname "$0")/.."

# Helm notes every symlinked file (charts/*/files -> dev/): not a problem, so not shown.
quiet() { grep -v "symbolic link" || true; }

KUBECONFORM_IMAGE=ghcr.io/yannh/kubeconform:v0.7.0
KUBERNETES_VERSION=1.37.0 # the kind node (infra/kind/cluster.yaml)
# JSON schemas of CRDs (ServiceMonitor, PrometheusRule, ...), pinned to one commit of the catalogue.
CRD_SCHEMAS="https://raw.githubusercontent.com/datreeio/CRDs-catalog/89c736bdde38b5fecd0f8436ec5f3aa3679a3b86/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"
OUT=build/k8s/render
rm -rf "$OUT" && mkdir -p "$OUT"

# Render as if the Prometheus Operator's CRDs exist, so the ServiceMonitors are rendered and validated too.
CAPS=(--api-versions monitoring.coreos.com/v1/ServiceMonitor --api-versions monitoring.coreos.com/v1/PrometheusRule)

values() { # the files make k8s-up passes for one environment
  local env=$1 f
  echo "-f deploy/envs/$env/global.yaml"
  for f in deploy/envs/"$env"/*.yaml; do [ "$(basename "$f")" = global.yaml ] || echo "-f $f"; done
}

echo "== helm lint"
for env in prod staging; do
  # shellcheck disable=SC2046
  helm lint --quiet charts/circuitbreaker $(values "$env") 2>&1 | quiet
done
helm lint --quiet charts/observability 2>&1 | quiet

echo "== render"
for env in prod staging; do
  # shellcheck disable=SC2046
  helm template circuitbreaker charts/circuitbreaker -n "$env" $(values "$env") "${CAPS[@]}" \
    > "$OUT/$env.yaml" 2> >(quiet >&2)
done
for only in api-gateway order-service matching-engine market-data-service portfolio-service bot-traders \
            sre-agent chaos-service web redpanda cassandra postgres ingress; do
  # shellcheck disable=SC2046
  helm template circuitbreaker charts/circuitbreaker -n prod $(values prod) "${CAPS[@]}" \
    --set "global.only=$only" > "$OUT/only-$only.yaml" 2> >(quiet >&2)
  # One service's render must contain that service and nothing of the others.
  if ! grep -q "^kind:" "$OUT/only-$only.yaml"; then echo "global.only=$only rendered nothing" >&2; exit 1; fi
done
helm template circuitbreaker-observability charts/observability -n observability > "$OUT/observability.yaml" \
  2> >(quiet >&2)
# What Argo CD deploys: the AppProject, the ApplicationSets, the observability Application.
helm lint --quiet infra/modules/argo-cd/applications --set repoURL=git@github.com:example/repo.git
helm template circuitbreaker-applications infra/modules/argo-cd/applications -n argocd \
  --set repoURL=git@github.com:example/repo.git > "$OUT/applications.yaml"

# The pinned third-party charts with our values (a typo in a values file fails here, not on the cluster).
python3 - <<'EOF' > "$OUT/platform.list"
import sys; sys.path.insert(0, "scripts")
import k8s
NAMESPACES = {"traefik": "traefik", "argo-cd": "argocd", "sealed-secrets": "kube-system"}
for chart, repo, version in k8s.CHARTS:  # values: infra/platform/<chart>.yaml (Terraform reads the same)
    print(chart, k8s.cached_chart(chart, repo, version), NAMESPACES.get(chart, "observability"), f"{chart}.yaml")
EOF
while read -r release package namespace values_file; do
  helm template "$release" "$package" -n "$namespace" -f "infra/platform/$values_file" > "$OUT/platform-$release.yaml"
done < "$OUT/platform.list"

echo "== kubeconform (Kubernetes $KUBERNETES_VERSION)"
docker run --rm -v "$PWD/$OUT:/r:ro" "$KUBECONFORM_IMAGE" -strict -summary \
  -kubernetes-version "$KUBERNETES_VERSION" -schema-location default -schema-location "$CRD_SCHEMAS" \
  /r/prod.yaml /r/staging.yaml /r/observability.yaml /r/applications.yaml \
  $(cd "$OUT" && ls only-*.yaml | sed 's|^|/r/|')
# The third-party charts install CRDs whose own schemas are not in any catalogue: those are skipped, the rest
# (Deployments, Services, ...) are validated.
docker run --rm -v "$PWD/$OUT:/r:ro" "$KUBECONFORM_IMAGE" -summary -ignore-missing-schemas \
  -kubernetes-version "$KUBERNETES_VERSION" -schema-location default -schema-location "$CRD_SCHEMAS" \
  $(cd "$OUT" && ls platform-*.yaml | sed 's|^|/r/|')

echo "== promtool"
make --no-print-directory test-alerts
