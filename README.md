# DriftGate

DriftGate is a release gate for self-hosted LLM serving on llama.cpp. It answers one operational question: "Did this change to the serving stack change model behavior, and should it ship?" It has two layers: an evidence layer on the laptop (fault injection, A/A calibration and a fault-by-detector matrix, with Prometheus, Grafana and OpenTelemetry), and a delivery layer on AWS Academy Learner Lab (CodeCommit push to EventBridge to a Lambda orchestrator, an SSM-driven EC2 build host, SNS approval, and a CodeDeploy rolling deploy whose `ValidateService` hook runs live behavioral checks and rolls back automatically on failure or alarm).

**Status:** Skeleton only, T-03.

## Commands

| Command | What it does |
| --- | --- |
| `make setup` | Create the environment and install dependencies |
| `make test` | Run the test suite |
| `make lint` | Run ruff (lint and format check) |
| `make fmt` | Auto-format |

See `PRD.md` for requirements and `docs/architecture.md` for the design.
