# CircuitBreaker developer entry point. `make help` lists the targets.
# Works with the GNU Make 3.81 that ships with macOS (no newer syntax used).
SHELL := /bin/bash

SERVICES := api-gateway order-service matching-engine market-data-service portfolio-service bot-traders sre-agent chaos-service
# s=<service> restricts per-service targets (test, lint, fmt, build, logs) to one service.
SEL      := $(if $(s),$(s),$(SERVICES))
# Services with integration tests (real Cassandra/Postgres/Redpanda via testcontainers) and property tests.
INTEGRATION := matching-engine portfolio-service order-service
PROPERTY    := matching-engine portfolio-service
COMPOSE  := docker compose
ALL      := --profile infra --profile app
REDOCLY  := npx --yes @redocly/cli@1.34.3
GATEWAY  := http://localhost:8080
# The dev admin passcode from docker-compose.yml (a local placeholder, never a real secret).
ADMIN_PASSCODE ?= cb-dev-admin-passcode

.DEFAULT_GOAL := help
.PHONY: help doctor setup infra up down reset demo-reset ps logs build lint fmt test test-integration test-deep test-e2e demo-trade status

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

ps: ## Show container status
	$(COMPOSE) $(ALL) ps -a

logs: ## Follow logs: make logs s=<service> (default: all)
	$(COMPOSE) $(ALL) logs -f --tail=100 $(s)

build: ## Build the Docker images: make build [s=<service>]
	$(COMPOSE) $(ALL) build $(s)

lint: ## ruff, mypy, redocly, template-drift and event-contract checks, web eslint/prettier
	@set -e; for s in $(SEL); do echo "== lint $$s"; \
	  (cd services/$$s && uv run --frozen ruff check --no-cache . && uv run --frozen ruff format --check --no-cache . && uv run --frozen mypy); done
	$(REDOCLY) lint
	python3 tools/check_template_drift.py
	uv run --quiet --locked tools/check_event_contracts.py
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

test-e2e: ## End-to-end checks on the running stack: trade, kill -9 the engine, orphan rule (~3 min; needs make up)
	@python3 scripts/e2e_test.py

status: ## Ask the gateway for the combined system status (signs in as admin first)
	@token=$$(curl -sS --max-time 10 -X POST $(GATEWAY)/v1/admin/sessions -H 'Content-Type: application/json' \
	  -d '{"nickname":"presenter","passcode":"$(ADMIN_PASSCODE)"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])') && \
	  curl -sS --max-time 10 -H "Authorization: Bearer $$token" $(GATEWAY)/v1/system/status | python3 -m json.tool
