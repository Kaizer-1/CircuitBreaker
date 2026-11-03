# CircuitBreaker developer entry point. `make help` lists the targets.
# Works with the GNU Make 3.81 that ships with macOS (no newer syntax used).
SHELL := /bin/bash

SERVICES := api-gateway order-service matching-engine market-data-service portfolio-service bot-traders sre-agent chaos-service
# s=<service> restricts per-service targets (test, lint, fmt, build, logs) to one service.
SEL      := $(if $(s),$(s),$(SERVICES))
COMPOSE  := docker compose
ALL      := --profile infra --profile app
REDOCLY  := npx --yes @redocly/cli@1.34.3
GATEWAY  := http://localhost:8080

.DEFAULT_GOAL := help
.PHONY: help doctor setup infra up down reset ps logs build lint fmt test status

help: ## List the available targets
	@echo "CircuitBreaker: make <target> [s=<service>]"
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

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

ps: ## Show container status
	$(COMPOSE) $(ALL) ps -a

logs: ## Follow logs: make logs s=<service> (default: all)
	$(COMPOSE) $(ALL) logs -f --tail=100 $(s)

build: ## Build the Docker images: make build [s=<service>]
	$(COMPOSE) $(ALL) build $(s)

lint: ## ruff, mypy, redocly, template-drift and event-contract checks, web eslint/prettier
	@set -e; for s in $(SEL); do echo "== lint $$s"; \
	  (cd services/$$s && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy); done
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

status: ## Ask the gateway for the combined system status
	@curl -sS --max-time 10 $(GATEWAY)/v1/system/status | python3 -m json.tool
