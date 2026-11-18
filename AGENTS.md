# AGENTS.md: DriftGate

Context for coding agents working in this repository. Read this file and `PRD.md` before every task.

## What this project is

DriftGate is a release gate for self-hosted LLM serving on llama.cpp. It answers: "Did this change to the serving stack change model behavior, and should it ship?"

- **Evidence layer (runs on the laptop):** fault injection (F1 to F9), A/A calibration and the fault-by-detector matrix. Prometheus, Grafana and OpenTelemetry run here too.
- **Delivery layer (runs on AWS Academy Learner Lab):** CodeCommit push → EventBridge → Lambda orchestrator → EC2 build host via SSM (tests, hashes, D1, D2) → SNS approval → CodeDeploy rolling deploy to 2 EC2 behind an ALB, with `ValidateService` running D3, D5, D6 and automatic rollback on hook failure or CloudWatch alarm.

`PRD.md` is the source of truth for requirements (FR-xx, NFR-xx), acceptance criteria and decisions (ADR-xxx). If a task conflicts with the PRD, stop and ask.

## Your role

The human owner makes design decisions; you implement the task you were given against its acceptance criteria.

- Implement only what the task describes. If the spec is ambiguous or missing a decision, stop and ask. Do not pick a design, threshold, library or model on your own.
- Do not do research tasks (choosing between approaches, surveying tools). Report the gap instead.
- Keep diffs small and scoped to the task. No drive-by refactors.

## Hard rules (never break these)

1. Never edit anything under `docs/prereg/` after the pre-registration commit exists.
2. Never change detector thresholds by hand. Thresholds come only from the calibration script output.
3. Never commit credentials, AWS keys, `.env` files, or Lab session tokens.
4. Never commit model weights or large artifacts (`*.gguf`, logits files). They live in `models/` and `artifacts/` (gitignored) or S3.
5. Keep detectors deterministic given a seed. Every randomized step takes an explicit `--seed`.
6. Run `make test` and `make lint` before proposing a diff. Both must pass.
7. Tag every commit message and PR title with the task id, e.g. `T-04: add SHA-256 manifest check`.
8. Never call AWS or create cloud resources from a task unless the task explicitly says so.

## Environment

- Development runs in WSL2 Ubuntu. The repo lives under the Linux home folder (`~/driftgate`), not `/mnt/c`.
- Python 3.11+, managed with `uv`. Use `uv run` for every Python command.
- llama.cpp is built locally from source; its binaries are found through `LLAMA_CPP_BIN` (default `~/llama.cpp/build/bin`).
- CPU only. Do not add GPU code paths.

## Commands

| Command | What it does |
| --- | --- |
| `make setup` | Create the environment and install dependencies |
| `make test` | Run the test suite |
| `make lint` | Run ruff (lint and format check) |
| `make fmt` | Auto-format |

More targets (`make serve`, `make gate`, `make fault F=F4`, `make up`, `make down`) are added by later tasks.

## Repository layout

```
driftgate/
  AGENTS.md            # this file
  PRD.md               # requirements, acceptance criteria, decisions
  GLOSSARY.md          # domain terms
  ATTRIBUTION.md       # third-party code, tools and models
  docs/adr/            # architecture decision records
  docs/prereg/         # pre-registration (frozen after its commit)
  docs/lab-constraints.md  # Lab Constraints and Equivalences appendix (FR-28)
  docs/architecture.md
  gate/                # detectors D1..D6, CLI, JSON verdicts
  faults/              # fault injector and fault definitions
  analysis/            # calibration and matrix scripts
  orchestrator/        # Lambda orchestrator and approval handler
  serving/             # serving config, chat templates, manifest
  deploy/              # build.sh, appspec.yml, hooks/, cloudformation/
  observability/       # prometheus, grafana, otel, CloudWatch dashboard
  iam/                 # least-privilege policies (documented; enforced where the Lab allows)
  tests/               # tests named by requirement, e.g. test_fr02_manifest.py
  .github/workflows/   # portable CI
```

## Conventions

- One branch per task: `t-NN-short-name`.
- Tests are keyed by requirement id in the file name (`tests/test_fr06_logprob_agreement.py`). Each acceptance criterion in a task maps to at least one test or a documented manual check.
- Gate and detector output is JSON with a stable schema; never print-only.
- Paths, ports, model names and thresholds come from config files, not hard-coded values.
- Commit messages: `T-NN: <imperative summary>`.
