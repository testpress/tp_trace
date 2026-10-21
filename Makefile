.DEFAULT_GOAL := help
PYTHON ?= python3
VENV ?= .venv
COMPOSE ?= docker compose

.PHONY: help install test build check-config up rebuild restart down logs ps smoke traffic

help: ## Show development commands
	@awk 'BEGIN {FS = ":.*## "} /^[a-z-]+:.*## / {printf "  %-16s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## Create a virtualenv and install the SDK with development dependencies
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/python -m ensurepip --upgrade
	$(VENV)/bin/python -m pip install -e '.[dev]'

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

logs: ## Follow stack logs (SERVICE=sample-app to filter)
	$(COMPOSE) logs -f $(SERVICE)

ps: ## Show stack status
	$(COMPOSE) ps

smoke: ## Verify host ingestion, storage, app dependencies, and Grafana
	$(PYTHON) tests/e2e/smoke.py

traffic: ## Generate sample-app traffic (ARGS='--help' for options; Ctrl+C to stop)
	bash sample-app/generate_traffic.sh $(ARGS)
