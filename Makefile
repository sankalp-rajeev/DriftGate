.DEFAULT_GOAL := help
.PHONY: help setup test lint fmt models verify serve

# Keep the project venv isolated from any PYTHONPATH set by the shell (e.g. a sourced ROS setup).
unexport PYTHONPATH

help: ## List make targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-8s %s\n", $$1, $$2}'

setup: ## Create the environment and install dependencies
	uv sync

test: ## Run the test suite
	uv run pytest -q

lint: ## Run ruff (lint and format check)
	uv run ruff check . && uv run ruff format --check .

fmt: ## Auto-format
	uv run ruff format . && uv run ruff check --fix .

models: ## Download pinned models into models/ and record SHA-256 in the manifest
	uv run python -m gate.models

verify: ## Check the served model against the SHA-256 manifest (FR-02)
	uv run python -m gate.manifest verify

serve: ## Run llama-server from serving/config.yaml (CONFIG=... to override)
	uv run python -m gate.serve --config $(or $(CONFIG),serving/config.yaml)
