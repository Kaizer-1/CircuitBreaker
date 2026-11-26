# CircuitBreaker developer entry point. `make help` lists the targets.
# Works with the GNU Make 3.81 that ships with macOS (no newer syntax used).
SHELL := /bin/bash

SERVICES := api-gateway order-service matching-engine market-data-service portfolio-service bot-traders sre-agent chaos-service
# s=<service> restricts per-service targets (test, lint, fmt, build, logs) to one service.
SEL      := $(if $(s),$(s),$(SERVICES))
# Services with integration tests (real Cassandra/Postgres/Redpanda via testcontainers) and property tests.
INTEGRATION := matching-engine portfolio-service order-service market-data-service
PROPERTY    := matching-engine portfolio-service market-data-service order-service
COMPOSE  := docker compose
ALL      := --profile infra --profile app
REDOCLY  := npx --yes @redocly/cli@1.34.3
GATEWAY  := http://localhost:8080
# The dev admin passcode from docker-compose.yml (a local placeholder, never a real secret).
ADMIN_PASSCODE ?= cb-dev-admin-passcode
# CI's image scanner, the same version (.github/workflows/ci.yml), pinned by digest.
TRIVY_IMAGE := aquasec/trivy:0.76.2@sha256:8f6bb947dbc528902e11f2f5be52f7b519f6a39d5d148f41166f2040499768fb

.DEFAULT_GOAL := help
.PHONY: help doctor setup infra up down reset demo-reset bots bots-stop mock ps logs build lint fmt test test-integration test-deep test-e2e demo-trade status \
        k8s-up k8s-down k8s-status k8s-set-url k8s-images k8s-reset k8s-bots k8s-bots-stop k8s-demo-trade k8s-passcode k8s-test-phone \
        test-alerts test-k8s k8s-check k8s-lint test-phone \
        platform platform-plan sync argocd seal sealing-cert sealing-key-backup ghcr-token openrouter-key github-app github-rules-check agent-eval agent-decide k8s-prepull scan promote

help: ## List the available targets
	@echo "CircuitBreaker: make <target> [s=<service>]"
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_-]+:.*## / {printf "  \033[36m%-17s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

doctor: ## Check prerequisites and print their versions
	@bash scripts/doctor.sh

setup: ## Install dependencies for every service and the web app, plus git hooks
	@set -e; for s in $(SERVICES); do echo "== uv sync $$s"; (cd services/$$s && uv sync --frozen); done
	@if [ -d web ]; then echo "== npm ci web"; (cd web && npm ci); fi
	pre-commit install

infra: ## Start only the infrastructure (Redpanda, Cassandra, Postgres + init jobs)
	$(COMPOSE) --profile infra up -d
	@python3 scripts/wait-stack.py infra

up: ## Build and start everything (infrastructure, 8 services, web)
	$(COMPOSE) $(ALL) up -d --build
	@python3 scripts/wait-stack.py infra app
	@echo; echo "System Status page: http://localhost:5173   Gateway: $(GATEWAY)   Redpanda Console: http://localhost:8088"

down: ## Stop everything (data volumes are kept)
	$(COMPOSE) $(ALL) down --remove-orphans

reset: ## Stop everything and WIPE all data volumes (asks for confirmation)
	@read -r -p "This deletes ALL local data (Cassandra, Postgres, Redpanda). Type 'yes' to continue: " a; \
	  [ "$$a" = "yes" ] || { echo "Aborted."; exit 1; }
	$(COMPOSE) $(ALL) down -v --remove-orphans

demo-reset: ## Rehearsal start: wipe ALL data, start everything, check the clean state (asks; CONFIRM=yes skips)
	@[ "$(CONFIRM)" = "yes" ] || { read -r -p "This deletes ALL local data and restarts the stack. Type 'yes' to continue: " a; \
	  [ "$$a" = "yes" ] || { echo "Aborted."; exit 1; }; }
	$(COMPOSE) $(ALL) down -v --remove-orphans
	$(COMPOSE) $(ALL) up -d --build
	@python3 scripts/wait-stack.py infra app
	@python3 scripts/demo_reset_check.py

bots: ## Start the 20 bots trading (needs make up): make bots [seed=42] [activity=1.0]
	BOTS_ENABLED=true BOT_RANDOM_SEED=$(seed) BOT_ACTIVITY=$(if $(activity),$(activity),1.0) \
	  $(COMPOSE) $(ALL) up -d --no-deps bot-traders
	@python3 scripts/wait-stack.py infra app
	@echo "20 bots trading (seed: $(if $(seed),$(seed),random), activity: $(if $(activity),$(activity),1.0)). Stop: make bots-stop"

bots-stop: ## Stop the bots trading (the container keeps running, idle)
	BOTS_ENABLED=false $(COMPOSE) $(ALL) up -d --no-deps bot-traders
	@python3 scripts/wait-stack.py infra app

mock: ## The public API mocked from the specs, for web work without the backend (Prism :4010, WebSockets :4011)
	@uv run --quiet --locked tools/mock_spec.py
	@trap 'kill 0' EXIT; \
	  npx --yes @stoplight/prism-cli@5.16.0 mock web/mock/build/api/api-gateway.yaml --port 4010 & \
	  node web/mock/ws-mock.mjs

ps: ## Show container status
	$(COMPOSE) $(ALL) ps -a

logs: ## Follow logs: make logs s=<service> (default: all)
	$(COMPOSE) $(ALL) logs -f --tail=100 $(s)

build: ## Build the Docker images: make build [s=<service>]
	$(COMPOSE) $(ALL) build $(s)

lint: ## ruff, mypy, redocly, template-drift, event-contract, retired-name and sealed-secret checks, web eslint/prettier
	@set -e; for s in $(SEL); do echo "== lint $$s"; \
	  (cd services/$$s && uv run --frozen ruff check --no-cache . && uv run --frozen ruff format --check --no-cache . && uv run --frozen mypy); done
	$(REDOCLY) lint
	python3 tools/check_template_drift.py
	uv run --quiet --locked tools/check_event_contracts.py
	python3 tools/check_retired_names.py
	python3 tools/check_sealed_secrets.py
	@if [ -d web/node_modules ] && [ -z "$(s)" ]; then echo "== lint web"; (cd web && npm run lint && npm run format:check); fi

fmt: ## Auto-format Python (ruff) and web (prettier): make fmt [s=<service>]
	@set -e; for s in $(SEL); do echo "== fmt $$s"; \
	  (cd services/$$s && uv run --frozen ruff check --fix . && uv run --frozen ruff format .); done
	@if [ -d web/node_modules ] && [ -z "$(s)" ]; then (cd web && npm run format); fi

test: ## Run the tests (incl. contract tests): make test [s=<service>]
	@set -e; for s in $(SEL); do echo "== test $$s"; (cd services/$$s && uv run --frozen pytest); done
	@if [ -d web/node_modules ] && [ -z "$(s)" ]; then echo "== test web"; (cd web && npm test); fi

test-integration: ## Integration tests against real stores in Docker: make test-integration [s=<service>]
	@set -e; for s in $(if $(s),$(s),$(INTEGRATION)); do echo "== integration $$s"; \
	  (cd services/$$s && uv run --frozen pytest -m integration --no-cov); done

test-deep: ## Property tests at 25,000 examples each (slow): make test-deep [s=<service>]
	@set -e; for s in $(if $(s),$(s),$(PROPERTY)); do echo "== deep properties $$s"; \
	  (cd services/$$s && HYPOTHESIS_PROFILE=deep uv run --frozen pytest --no-cov tests/property); done

demo-trade: ## Two traders and a bot trade through the gateway; prints trades, portfolios, orders (needs make up)
	@python3 scripts/demo_trade.py

test-phone: ## The phone on a non-secure origin (Playwright, 390x844, both skins; needs bots): make test-phone [url=http://localhost:5173]
	@cd web && node scripts/phone_check.mjs $(if $(url),$(url),http://localhost:5173)

test-e2e: ## End-to-end checks on the running stack: trade, kill -9 the engine, orphan rule (~3 min; needs make up)
	@python3 scripts/e2e_test.py

k8s-lint: ## Charts and alert rules without a cluster: helm lint, render, kubeconform, promtool (needs Docker)
	@bash scripts/k8s_lint.sh

PROMETHEUS_IMAGE := quay.io/prometheus/prometheus:v3.16.0@sha256:9332054523223e2ddc18d8101799d54a4a8d823b030c53216f063a6c117773cd
test-alerts: ## promtool: check the alert rules and run their unit tests (observability/tests; needs Docker)
	docker run --rm -v "$(CURDIR)/observability:/obs:ro" -w /obs/tests --entrypoint promtool $(PROMETHEUS_IMAGE) \
	  check rules /obs/alerts/services.yaml /obs/alerts/platform.yaml
	docker run --rm -v "$(CURDIR)/observability:/obs:ro" -w /obs/tests --entrypoint promtool $(PROMETHEUS_IMAGE) \
	  test rules alerts_test.yaml

status: ## Ask the gateway for the combined system status (signs in as admin first)
	@token=$$(curl -sS --max-time 10 -X POST $(GATEWAY)/v1/admin/sessions -H 'Content-Type: application/json' \
	  -d '{"nickname":"presenter","passcode":"$(ADMIN_PASSCODE)"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])') && \
	  curl -sS --max-time 10 -H "Authorization: Bearer $$token" $(GATEWAY)/v1/system/status | python3 -m json.tool

# --------------------------------------------------------------------------- Platform as code, GitOps (Phase 6)
# Terraform builds the platform (infra/envs/local, ADR-0015); Argo CD deploys staging and prod from Git (ADR-0014);
# Secrets are SealedSecrets in Git (ADR-0019). envs= limits which environments Argo CD manages (default both).

TF := terraform -chdir=infra/envs/local
TF_VARS = $(if $(envs),-var 'gitops_environments=[$(foreach e,$(envs),"$(e)",)]',) $(if $(revision),-var 'target_revision=$(revision)',)

platform-plan: ## Platform: show what make platform would change (Terraform plan) [envs="staging prod"] [revision=<branch>]
	@python3 scripts/k8s.py charts
	@$(TF) init -input=false >/dev/null
	@$(TF) plan $(TF_VARS)

platform: ## Platform: cluster, namespaces, Traefik, monitoring, Sealed Secrets, Argo CD from nothing (plan, ask, apply)
	@python3 scripts/k8s.py charts
	@$(TF) init -input=false >/dev/null
	@$(TF) apply $(TF_VARS)
	@python3 scripts/k8s.py argo-agent-token

sync: ## GitOps: ask Argo CD to look at Git now (it polls every 30 s): make sync [env=staging] [s=<service>] [apply=yes]
	@python3 scripts/k8s.py sync --env $(if $(env),$(env),staging) $(s) $(if $(filter yes,$(apply)),--apply,)

promote: ## GitOps: a pull request giving prod the images staging runs (merging it is the release): make promote s="<svc> ..."|all [dry=yes]
	@test -n "$(s)" || { echo 'usage: make promote s="order-service web" (or s=all) [dry=yes]'; exit 2; }
	@python3 scripts/gitops_images.py promote $(s) $(if $(filter yes,$(dry)),--dry-run,)

argocd: ## GitOps: Argo CD's web UI on http://localhost:8443 (prints the admin password)
	@python3 scripts/k8s.py argocd

seal: ## Secrets: seal an environment's secrets into deploy/envs/<env>/sealed/: make seal env=prod [rotate="jwt-secret ..."]
	@test -n "$(env)" || { echo "usage: make seal env=staging|prod [rotate=\"<secret> ...\"]"; exit 2; }
	@python3 scripts/sealed_secrets.py seal --env $(env) $(if $(rotate),--rotate $(rotate),)

sealing-cert: ## Secrets: save the controller's PUBLIC certificate (infra/sealed-secrets/sealing-cert.pem, committed)
	@python3 scripts/sealed_secrets.py cert

sealing-key-backup: ## Secrets: save the controller's PRIVATE sealing key to private/ (a person runs it; copy it off the laptop)
	@python3 scripts/sealed_secrets.py backup

ghcr-token: ## Secrets: seal the GHCR pull token for both environments (a person pastes a classic read:packages token)
	@python3 scripts/sealed_secrets.py ghcr-token

openrouter-key: ## Secrets: seal the SRE agent's OpenRouter key for both environments (a person pastes it; it must have a credit limit)
	@python3 scripts/sealed_secrets.py openrouter-key

github-app: ## Secrets: check and seal the SRE agent's GitHub App (ID asked for; key from private/keys/): make github-app [app_id=] [pem=]
	@python3 scripts/sealed_secrets.py github-app $(if $(app_id),--app-id $(app_id),) $(if $(pem),--pem $(pem),)

agent-eval: ## SRE agent: 21 designed situations through the real model, 3 runs each (~200k tokens): make agent-eval [runs=3]
	@cd services/sre-agent && uv run --frozen python -m eval.run --runs $(if $(runs),$(runs),3)

github-rules-check: ## Secrets: GitHub must refuse the agent's App a merge, a push to main and any v* tag change, and everyone a v* tag delete or move
	@python3 scripts/github_rules_check.py

# --------------------------------------------------------------------------- Kubernetes (kind), Phase 5
# The demo platform. Compose stays the fast developer loop. make k8s-up develops with local images in a namespace
# Argo CD does not manage; in one it does, it only starts the cluster and waits for Argo CD.

k8s-up: ## Kubernetes: images + the chart into an environment Argo CD does not manage (env=prod|staging); PUBLIC_BASE_URL=http://<LAN IP> sets the QR code
	@python3 scripts/k8s.py up --env $(if $(env),$(env),prod)

k8s-down: ## Kubernetes: stop the cluster (data kept, RAM freed; k8s-up starts it again)
	@python3 scripts/k8s.py down

k8s-status: ## Kubernetes: pods in every namespace, the cluster's memory, the URL in the join QR code
	@python3 scripts/k8s.py status

k8s-set-url: ## Kubernetes: point the join QR code at a new address, data kept: make k8s-set-url url=http://<LAN IP> [env=]
	@test -n "$(url)" || { echo "usage: make k8s-set-url url=http://<this laptop's LAN IP> [env=staging]"; exit 2; }
	@python3 scripts/k8s.py set-url --env $(if $(env),$(env),prod) "$(url)"

k8s-images: ## Kubernetes: build changed images and push them to the local registry: make k8s-images [s=<service>]
	@python3 scripts/k8s.py images $(s)

k8s-prepull: ## Kubernetes: put every image the demo needs on the node (the day before; the hotspot has no internet)
	@python3 scripts/k8s.py prepull

scan: ## CI's image gate on this laptop: build an image, Trivy fails on a CRITICAL with a fix: make scan s=<service|web>
	@test -n "$(s)" || { echo "usage: make scan s=<service|web>"; exit 2; }
	@docker build -q --pull -t circuitbreaker-scan/$(s):local $(if $(filter web,$(s)),web,services/$(s)) >/dev/null
	@docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v circuitbreaker-trivy-cache:/root/.cache $(TRIVY_IMAGE) \
	  image --quiet --scanners vuln --severity HIGH,CRITICAL --ignore-unfixed circuitbreaker-scan/$(s):local
	@docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v circuitbreaker-trivy-cache:/root/.cache $(TRIVY_IMAGE) \
	  image --quiet --scanners vuln --severity CRITICAL --ignore-unfixed --exit-code 1 --skip-db-update circuitbreaker-scan/$(s):local \
	  && echo "scan $(s): no CRITICAL vulnerability with a fix"

k8s-reset: ## Kubernetes: DELETE the cluster and all its data (asks; run by a person, never a script)
	@python3 scripts/k8s.py reset

k8s-bots: ## Kubernetes: start the 20 bots trading: make k8s-bots [env=] [seed=42] [activity=1.0]
	@python3 scripts/k8s.py bots --env $(if $(env),$(env),prod) --seed "$(seed)" --activity $(if $(activity),$(activity),1.0)

k8s-bots-stop: ## Kubernetes: stop the bots trading
	@python3 scripts/k8s.py bots --stop --env $(if $(env),$(env),prod)

k8s-passcode: ## Kubernetes: the presenter's admin passcode (random, sealed in Git): make k8s-passcode [env=]
	@python3 scripts/k8s.py secret --env $(if $(env),$(env),prod) admin-passcode

k8s-test-phone: ## Kubernetes: make test-phone against an environment, with its own bot secret: make k8s-test-phone [env=]
	@cd web && BOT_SECRET="$$(python3 ../scripts/k8s.py secret --env $(if $(env),$(env),prod) bot-secret)" \
	  node scripts/phone_check.mjs $(if $(filter staging,$(env)),http://localhost:8090,http://localhost)

k8s-demo-trade: ## Kubernetes: make demo-trade against the cluster (env=prod|staging)
	@python3 scripts/k8s.py run --env $(if $(env),$(env),prod) demo_trade.py

test-k8s: ## Kubernetes smoke test: k8s-up, every pod ready, demo trade, bots 2 min, trades/s > 0, Loki trace
	@python3 scripts/k8s.py up --env $(if $(env),$(env),prod)
	@python3 scripts/k8s_checks.py --env $(if $(env),$(env),prod) smoke

k8s-check: ## Kubernetes checks: make k8s-check c=netpol|kill|scale|alerts|url|chaos|agent [args="..."] (they change the system on purpose)
	@python3 scripts/k8s_checks.py --env $(if $(env),$(env),prod) $(c) $(args)

agent-decide: ## SRE agent: approve or decline its waiting proposal, as the presenter: make agent-decide d=approve|decline [env=staging] [comment="..."]
	@python3 scripts/k8s_checks.py --env $(if $(env),$(env),staging) decide $(d) $(if $(comment),--comment "$(comment)",)
