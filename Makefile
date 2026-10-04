# =============================================================================
#  PAS Plugins - developer entry points
#  Every target is safe to re-run and works from a clean clone.
# =============================================================================
SHELL := /bin/bash
.DEFAULT_GOAL := help
VENV   ?= .venv
PY     := $(VENV)/Scripts/python
PIP    := $(VENV)/Scripts/pip
UVICORN:= $(VENV)/Scripts/uvicorn
PYTEST := $(VENV)/Scripts/pytest
RUFF   := $(VENV)/Scripts/ruff
MYPY   := $(VENV)/Scripts/mypy

PLUGINS := 1 2 3 4 5 6 7
API_DIRS := $(foreach p,$(PLUGINS),src/pas_plugins/plugin$(p)/*)

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS=":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# --- setup ------------------------------------------------------------------
$(VENV):
	python3.12 -m venv $(VENV) || python -m venv $(VENV)

.PHONY: install
install: $(VENV) ## Create venv and install all runtime + dev dependencies
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"
	$(MAKE) ui-install

.PHONY: ui-install
ui-install: ## Install JS dependencies for every Svelte management UI
	@for d in ui/*/; do \
	  if [ -f "$$d/package.json" ]; then echo "==> npm ci in $$d"; (cd "$$d" && npm install --silent); fi; \
	done

.PHONY: env
env: ## Create .env from the template
	@test -f .env || (cp .env.example .env && echo "created .env")

# --- quality ----------------------------------------------------------------
.PHONY: lint
lint: ## Ruff lint (auto-fix)
	$(RUFF) check --fix $(API_DIRS) tests

.PHONY: lint-check
lint-check: ## Ruff lint (check only, used in CI)
	$(RUFF) check $(API_DIRS) tests

.PHONY: fmt
fmt: ## Ruff formatter
	$(RUFF) format $(API_DIRS) tests

.PHONY: typecheck
typecheck: ## Mypy type check on the shared core library
	$(MYPY)

.PHONY: check
check: lint-check typecheck test ## Everything CI runs

# --- tests ------------------------------------------------------------------
.PHONY: test
test: ## Unit + integration tests
	$(PYTEST)

.PHONY: test-cov
test-cov: ## Tests with coverage report
	$(PYTEST) --cov --cov-report=term-missing --cov-report=html

.PHONY: test-contract
test-contract: ## Schemathesis contract tests over the committed OpenAPI documents
	$(PY) scripts/contract_tests.py

.PHONY: test-load
test-load: ## k6 load test for the plugin 5 sub-second quote/bind SLA
	k6 run tests/load/quote_bind.js

# --- run --------------------------------------------------------------------
.PHONY: run-p1
run-p1: ## Run plugin 1 (API gateway + MCP orchestrator) on :8001
	$(UVICORN) pas_plugins.plugin1_gateway.main:app --port 8001 --reload

.PHONY: run-p2
run-p2: ## Run plugin 2 (IFRS 17) on :8002
	$(UVICORN) pas_plugins.plugin2_ifrs17.main:app --port 8002 --reload

.PHONY: run-p3
run-p3: ## Run plugin 3 (AUW workbench) on :8003
	$(UVICORN) pas_plugins.plugin3_auw.main:app --port 8003 --reload

.PHONY: run-p4
run-p4: ## Run plugin 4 (low-code product config) on :8004
	$(UVICORN) pas_plugins.plugin4_productconfig.main:app --port 8004 --reload

.PHONY: run-p5
run-p5: ## Run plugin 5 (embedded distribution) on :8005
	$(UVICORN) pas_plugins.plugin5_embedded.main:app --port 8005 --reload

.PHONY: run-p6
run-p6: ## Run plugin 6 (data mesh) on :8006
	$(UVICORN) pas_plugins.plugin6_datamesh.main:app --port 8006 --reload

.PHONY: run-p7
run-p7: ## Run plugin 7 (blockchain lifecycle) on :8007
	$(UVICORN) pas_plugins.plugin7_blockchain.main:app --port 8007 --reload

# --- contracts and generated artefacts ---------------------------------------
.PHONY: openapi
openapi: ## Export each plugin's OpenAPI 3.1 document to contracts/openapi/
	$(PY) scripts/export_openapi.py

.PHONY: openapi-check
openapi-check: ## Fail when a committed OpenAPI document is stale or invalid
	$(PY) scripts/export_openapi.py --check --validate

.PHONY: postman
postman: ## Generate Postman collections from the OpenAPI documents
	$(PY) scripts/generate_postman.py

.PHONY: ui-generate
ui-generate: ## Regenerate the seven Svelte + TypeScript UIs
	$(PY) scripts/generate_ui.py

.PHONY: generate
generate: openapi postman ui-generate ## Regenerate every committed artefact

.PHONY: generate-check
generate-check: openapi-check ## Fail when any committed artefact is stale

.PHONY: smoke
smoke: ## Import every plugin app and confirm it serves an OpenAPI document
	$(PY) scripts/smoke_plugins.py

# --- deploy -----------------------------------------------------------------
.PHONY: up
up: ## Start the full local stack (docker compose)
	docker compose -f deploy/docker-compose.yml up -d --build

.PHONY: down
down: ## Stop the local stack
	docker compose -f deploy/docker-compose.yml down -v

.PHONY: logs
logs: ## Tail local stack logs
	docker compose -f deploy/docker-compose.yml logs -f --tail=100

.PHONY: helm-lint
helm-lint: ## Lint all Helm charts
	@for c in deploy/helm/*/; do echo "==> $$c"; helm lint "$$c"; done

.PHONY: clean
clean: ## Remove caches and build artefacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
