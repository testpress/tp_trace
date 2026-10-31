.DEFAULT_GOAL := help
PYTHON ?= python3
VENV ?= .venv
COMPOSE ?= docker compose $(if $(wildcard .env),--env-file .env) -f dev/docker-compose.yml

.PHONY: help install lint test pre-commit build check-config up rebuild restart down nuke logs ps smoke traffic

help: ## Show development commands
	@awk 'BEGIN {FS = ":.*## "} /^[a-z-]+:.*## / {printf "  %-16s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## Create a virtualenv and install the SDK with development dependencies
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/python -m ensurepip --upgrade
	$(VENV)/bin/python -m pip install -e '.[dev]'

lint: ## Run linter and typechecker (ruff and mypy)
	$(VENV)/bin/ruff check src/ tests/
	$(VENV)/bin/mypy src/

format: ## Format Python source and tests
	$(VENV)/bin/ruff format src/ tests/

pre-commit: ## Run pre-commit hooks on all files
	$(VENV)/bin/pre-commit run --all-files

test: ## Run the SDK test suite (ARGS='-k name' to filter)
	$(VENV)/bin/python -m pytest $(ARGS)

build: ## Build wheel and source distribution
	$(VENV)/bin/python -m build

check-config: ## Validate the resolved local Compose configuration
	$(COMPOSE) config --quiet

up: ## Start the local stack using existing images (no build)
	$(COMPOSE) up --no-build -d --wait --wait-timeout 180

rebuild: ## Build images and start the local stack after dependency or Dockerfile changes
	$(COMPOSE) up --build -d --wait --wait-timeout 180

restart: ## Restart without building (SERVICE=sample-app to select one)
	$(COMPOSE) restart $(SERVICE)

down: ## Stop the local stack, preserving data volumes
	$(COMPOSE) down

nuke: ## Stop the stack and delete all data volumes (full reset; data is unrecoverable)
	$(COMPOSE) down --volumes --remove-orphans

logs: ## Follow stack logs (SERVICE=sample-app to filter)
	$(COMPOSE) logs -f $(SERVICE)

ps: ## Show stack status
	$(COMPOSE) ps

smoke: ## Verify host ingestion, storage, app dependencies, and Grafana
	$(PYTHON) tests/e2e/smoke.py

traffic: ## Generate a bounded sample workload (ARGS overrides the default)
	bash dev/sample-app/generate_traffic.sh $(if $(ARGS),$(ARGS),--count 20 --redis)
