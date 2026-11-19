.DEFAULT_GOAL := help
.PHONY: help setup test lint fmt models verify serve gate fault up down storage-up storage-down obs-up obs-down orchestrator-zip

# Keep the project venv isolated from any PYTHONPATH set by the shell (e.g. a sourced ROS setup).
unexport PYTHONPATH

# Per-session network stack (FR-21). `make up` / `make down` call AWS: run them only in a Lab session.
AWS_REGION ?= us-east-1
STACK_NAME ?= driftgate-net
# Persistent storage stack (PRD 6.8). Separate from the session stack: `make down` never touches it.
STORAGE_STACK_NAME ?= driftgate-storage
CFN_DIR := deploy/cloudformation
CFN_TEMPLATES := $(CFN_DIR)/*.yaml

help: ## List make targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-8s %s\n", $$1, $$2}'

setup: ## Create the environment and install dependencies
	uv sync

test: ## Run the test suite
	uv run pytest -q

lint: ## Run ruff (lint and format check)
	uv run ruff check . && uv run ruff format --check .
	uv run cfn-lint $(CFN_TEMPLATES)

fmt: ## Auto-format
	uv run ruff format . && uv run ruff check --fix .

models: ## Download pinned models into models/ and record SHA-256 in the manifest
	uv run python -m gate.models

verify: ## Check the served model against the SHA-256 manifest (FR-02)
	uv run python -m gate.manifest verify

gate: ## Run the gate against the served model (BUDGET=n SEED=n required; OUT=verdict path)
	uv run python -m gate run --budget $(BUDGET) --seed $(SEED) $(if $(OUT),--out $(OUT))

fault: ## Build a fault bundle and log its diff (F=AA|F1|F2|F3|F4, OUT=dir, default artifacts/bundles)
	uv run python -m faults.inject $(F) --out $(or $(OUT),artifacts/bundles)

serve: ## Run llama-server from serving/config.yaml (CONFIG=... to override)
	uv run python -m gate.serve --config $(or $(CONFIG),serving/config.yaml)

orchestrator-zip: ## Package the orchestrator Lambda (committed HEAD) to artifacts/orchestrator.zip
	mkdir -p artifacts && git archive --format=zip -o artifacts/orchestrator.zip HEAD orchestrator/

up: ## Create or update the per-session network stack (calls AWS; Lab only)
	aws cloudformation deploy --region $(AWS_REGION) --stack-name $(STACK_NAME) --template-file $(CFN_DIR)/network.yaml --parameter-overrides file://$(CFN_DIR)/network-params.json --tags Project=driftgate --no-fail-on-empty-changeset

down: ## Delete the per-session network stack and wait (calls AWS; Lab only)
	aws cloudformation delete-stack --region $(AWS_REGION) --stack-name $(STACK_NAME) && aws cloudformation wait stack-delete-complete --region $(AWS_REGION) --stack-name $(STACK_NAME)

storage-up: ## Create or update the persistent driftgate-storage stack (calls AWS; Lab only)
	aws cloudformation deploy --region $(AWS_REGION) --stack-name $(STORAGE_STACK_NAME) --template-file $(CFN_DIR)/storage.yaml --tags Project=driftgate --no-fail-on-empty-changeset

storage-down: ## Delete the storage stack; empty every object version first (calls AWS; teardown only)
	aws cloudformation delete-stack --region $(AWS_REGION) --stack-name $(STORAGE_STACK_NAME) && aws cloudformation wait stack-delete-complete --region $(AWS_REGION) --stack-name $(STORAGE_STACK_NAME)

OBS_COMPOSE := docker compose -f observability/docker-compose.yml

obs-up: ## Start Prometheus, Grafana, OTel collector and Jaeger locally (FR-22 to FR-24)
	mkdir -p runs/otel && $(OBS_COMPOSE) up -d

obs-down: ## Stop the local observability stack
	$(OBS_COMPOSE) down
