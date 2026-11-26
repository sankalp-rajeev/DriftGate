# DriftGate

DriftGate is a release gate for self-hosted LLM serving on llama.cpp. It answers one operational question: "Did this change to the serving stack change model behavior, and should it ship?" It has two layers: an evidence layer on the laptop (fault injection, A/A calibration and a fault-by-detector matrix, with Prometheus, Grafana and OpenTelemetry), and a delivery layer on AWS Academy Learner Lab (CodeCommit push to EventBridge to a Lambda orchestrator, an SSM-driven EC2 build host, SNS approval, and a CodeDeploy rolling deploy whose `ValidateService` hook runs live behavioral checks and rolls back automatically on failure or alarm).

Status: Lab pipeline pending.
The 25 November Learner Lab rehearsal completed the first end-to-end push through SNS approval
and CodeDeploy. D2 blocked the F1 quantization change during Build, and a CloudWatch alarm
stopped a rolling deployment and triggered a successful automatic rollback. See the
[session 3 results](results/2026-11-25-lab-session3/README.md).

## Commands

| Command | What it does |
| --- | --- |
| `make setup` | Create the environment and install dependencies |
| `make test` | Run the test suite |
| `make lint` | Run ruff (lint and format check) |
| `make fmt` | Auto-format |
| `make up` | Create the per-session network stack in the Lab (calls AWS) |
| `make down` | Delete the per-session network stack (calls AWS) |
| `make storage-up` | Create the persistent `driftgate-storage` S3 stack (calls AWS) |
| `make storage-down` | Delete the storage stack at final teardown (calls AWS; empty the bucket first) |

See `PRD.md` for requirements and `docs/architecture.md` for the design.
