.DEFAULT_GOAL := help
.PHONY: help setup test lint fmt models verify serve gate reference fault study study-status up down storage-up storage-down obs-up obs-down orchestrator-zip unlock
.PHONY: codecommit-push cd-up cd-down orch-up session-down lab-status
.PHONY: demo-push demo-reset session-up pipeline-status
.PHONY: check-baseline-fixture

# Keep the project venv isolated from any PYTHONPATH set by the shell (e.g. a sourced ROS setup).
STOP_AT ?=

unexport PYTHONPATH

# Per-session network stack (FR-21). `make up` / `make down` call AWS: run them only in a Lab session.
AWS_REGION ?= us-east-1
STACK_NAME ?= driftgate-net
# Persistent storage stack (PRD 6.8). Separate from the session stack: `make down` never touches it.
STORAGE_STACK_NAME ?= driftgate-storage
CD_STACK_NAME ?= driftgate-cd
ORCH_STACK_NAME ?= driftgate-orchestrator
REPOSITORY_NAME ?= driftgate
APPROVAL_EMAIL ?=
CFN_DIR := deploy/cloudformation
CFN_TEMPLATES := $(CFN_DIR)/*.yaml orchestrator/template.yaml

# Expanded in Lab recipes only. Resolve the storage output and the account of the active session.
LAB_BUCKET = aws cloudformation describe-stacks --region "$(AWS_REGION)" --stack-name "$(STORAGE_STACK_NAME)" --query "Stacks[0].Outputs[?OutputKey=='BucketName'].OutputValue | [0]" --output text
LAB_ACCOUNT = aws sts get-caller-identity --region "$(AWS_REGION)" --query Account --output text

help: ## List make targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-8s %s\n", $$1, $$2}'

setup: ## Create the environment and install dependencies
	uv sync

test: ## Run the test suite excluding CI-only candidate regressions
	uv run pytest -q -m "not candidate_suite"

check-baseline-fixture: ## Check baseline test data against the approved model and config hashes (CI only)
	uv run python -m analysis.check_baseline_fixture

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

reference: ## Measure the reference revision into reference/<model-sha256>/<config-hash>/<instance-type>/ (SEED=n; UPLOAD=1 for S3)
	uv run python -m gate reference --seed $(SEED) $(if $(INSTANCE_TYPE),--instance-type $(INSTANCE_TYPE)) $(if $(UPLOAD),--upload)

fault: ## Build a fault bundle and log its diff (F=AA|F1|F2|F3|F4, OUT=dir, default artifacts/bundles)
	uv run python -m faults.inject $(F) --out $(or $(OUT),artifacts/bundles)

serve: ## Run llama-server from serving/config.yaml (CONFIG=... to override)
	uv run python -m gate.serve --config $(or $(CONFIG),serving/config.yaml)

study: ## Overnight study, resumable and time-boxed (STOP_AT=07:00; needs the pre-registration on main)
	uv run python -m analysis.study --stop-at $(STOP_AT) $(if $(OUT),--out $(OUT))

study-status: ## Progress of the study
	uv run python -m analysis.study --status $(if $(OUT),--out $(OUT)); tail -n 5 $(if $(OUT),$(OUT),$$(ls -d runs/*-study | tail -1))/study.log

orchestrator-zip: ## Package the orchestrator Lambda (committed HEAD) to artifacts/orchestrator.zip
	mkdir -p artifacts && git archive --format=zip -o artifacts/orchestrator.zip HEAD orchestrator/

unlock: ## Clear a stuck run lock, marking its run abandoned (BUCKET=...; uses your AWS credentials, Lab only)
	uv run python -m orchestrator.unlock --bucket $(BUCKET)

up: ## Create or update the per-session network stack (calls AWS; Lab only)
	aws cloudformation deploy --region $(AWS_REGION) --stack-name $(STACK_NAME) --template-file $(CFN_DIR)/network.yaml --parameter-overrides file://$(CFN_DIR)/network-params.json --tags Project=driftgate --no-fail-on-empty-changeset

down: ## Delete the per-session network stack and wait (calls AWS; Lab only)
	aws cloudformation delete-stack --region $(AWS_REGION) --stack-name $(STACK_NAME) && aws cloudformation wait stack-delete-complete --region $(AWS_REGION) --stack-name $(STACK_NAME)

storage-up: ## Create or update the persistent driftgate-storage stack (calls AWS; Lab only)
	aws cloudformation deploy --region $(AWS_REGION) --stack-name $(STORAGE_STACK_NAME) --template-file $(CFN_DIR)/storage.yaml --tags Project=driftgate --no-fail-on-empty-changeset

storage-down: ## Delete the storage stack; empty every object version first (calls AWS; teardown only)
	aws cloudformation delete-stack --region $(AWS_REGION) --stack-name $(STORAGE_STACK_NAME) && aws cloudformation wait stack-delete-complete --region $(AWS_REGION) --stack-name $(STORAGE_STACK_NAME)

codecommit-push: ## Push local main to the Lab repository via git-remote-codecommit
	git push "codecommit::$(AWS_REGION)://$(REPOSITORY_NAME)" main:main

demo-push: ## Push a clean or injected-fault demo commit to CodeCommit (F=CLEAN|F1|F9|...; Lab only)
	@bash scripts/demo_push.sh "$(F)"

demo-reset: ## Push the exact origin/main tree to CodeCommit, preserving its history (Lab only)
	@bash scripts/demo_push.sh RESET

session-up: ## Start session stacks and wait up to 30 min for build host READY (APPROVAL_EMAIL required; Lab only)
	+@APPROVAL_EMAIL="$(APPROVAL_EMAIL)" AWS_REGION="$(AWS_REGION)" STACK_NAME="$(STACK_NAME)" \
		CD_STACK_NAME="$(CD_STACK_NAME)" ORCH_STACK_NAME="$(ORCH_STACK_NAME)" \
		STORAGE_STACK_NAME="$(STORAGE_STACK_NAME)" REPOSITORY_NAME="$(REPOSITORY_NAME)" \
		MAKE="$(MAKE)" bash scripts/lab_session_up.sh

pipeline-status: ## Print the newest pipeline run, stages, approval, deployment and build log URI (Lab only)
	@set -eu; bucket="$$($(LAB_BUCKET))"; \
	test -n "$$bucket" && test "$$bucket" != None; \
	uv run python -m orchestrator.status --bucket "$$bucket" --region "$(AWS_REGION)"

cd-up: ## Create or update the CodeDeploy stack and the three EC2 instances
	@set -eu; bucket="$$($(LAB_BUCKET))"; account="$$($(LAB_ACCOUNT))"; \
	test -n "$$bucket" && test "$$bucket" != None; \
	aws cloudformation deploy --region "$(AWS_REGION)" --stack-name "$(CD_STACK_NAME)" \
		--template-file "$(CFN_DIR)/codedeploy.yaml" --tags Project=driftgate --no-fail-on-empty-changeset \
		--parameter-overrides "NetworkStackName=$(STACK_NAME)" "BucketName=$$bucket" \
		"LabRoleArn=arn:aws:iam::$$account:role/LabRole" "RepositoryName=$(REPOSITORY_NAME)"

cd-down: ## Delete CodeDeploy and EC2 instances, then wait for stack deletion
	aws cloudformation delete-stack --region "$(AWS_REGION)" --stack-name "$(CD_STACK_NAME)" && \
		aws cloudformation wait stack-delete-complete --region "$(AWS_REGION)" --stack-name "$(CD_STACK_NAME)"

orch-up: ## Package and deploy the orchestrator and SNS subscription (APPROVAL_EMAIL required)
	@test -n "$(strip $(APPROVAL_EMAIL))" || { echo "APPROVAL_EMAIL is required" >&2; exit 2; }
	$(MAKE) orchestrator-zip
	@set -eu; bucket="$$($(LAB_BUCKET))"; account="$$($(LAB_ACCOUNT))"; \
	test -n "$$bucket" && test "$$bucket" != None; \
	key="lambda/orchestrator-$$(git rev-parse HEAD).zip"; \
	aws s3 cp --region "$(AWS_REGION)" --sse AES256 artifacts/orchestrator.zip "s3://$$bucket/$$key"; \
	aws cloudformation deploy --region "$(AWS_REGION)" --stack-name "$(ORCH_STACK_NAME)" \
		--template-file orchestrator/template.yaml --tags Project=driftgate --no-fail-on-empty-changeset \
		--parameter-overrides "RunBucketName=$$bucket" "CodeBucket=$$bucket" "CodeKey=$$key" \
		"LabRoleArn=arn:aws:iam::$$account:role/LabRole" "RepositoryName=$(REPOSITORY_NAME)" \
		"ApprovalEmail=$(APPROVAL_EMAIL)"

session-down: ## Delete the CodeDeploy stack before deleting the network stack
	$(MAKE) cd-down
	$(MAKE) down

lab-status: ## Show DriftGate stack statuses and EC2 instance states
	aws cloudformation describe-stacks --region "$(AWS_REGION)" --output table \
		--query "Stacks[?StackName=='$(STORAGE_STACK_NAME)' || StackName=='$(STACK_NAME)' || StackName=='$(CD_STACK_NAME)' || StackName=='$(ORCH_STACK_NAME)'].[StackName,StackStatus]"
	aws ec2 describe-instances --region "$(AWS_REGION)" --output table \
		--filters "Name=tag:Project,Values=driftgate" \
		--query "Reservations[].Instances[].[InstanceId,Tags[?Key=='Name'].Value | [0],State.Name]"

OBS_COMPOSE := docker compose -f observability/docker-compose.yml

obs-up: ## Start Prometheus, Grafana, OTel collector and Jaeger locally (FR-22 to FR-24)
	mkdir -p runs/otel && $(OBS_COMPOSE) up -d

obs-down: ## Stop the local observability stack
	$(OBS_COMPOSE) down
