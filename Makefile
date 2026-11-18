.DEFAULT_GOAL := help
.PHONY: help setup test lint fmt orchestrator-zip

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

orchestrator-zip: ## Package the orchestrator Lambda (committed HEAD) to artifacts/orchestrator.zip
	mkdir -p artifacts && git archive --format=zip -o artifacts/orchestrator.zip HEAD orchestrator/
