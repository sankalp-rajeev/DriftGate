# AGENTS.md: DriftGate

Context for coding agents working in this repository. Read this file and `PRD.md` before every task.

## What this project is

DriftGate is a release gate for self-hosted LLM serving on llama.cpp. It answers: "Did this change to the serving stack change model behavior, and should it ship?"

- **Evidence layer (runs on the laptop):** fault injection (F1 to F9), A/A calibration and the fault-by-detector matrix. Prometheus, Grafana and OpenTelemetry run here too.
- **Delivery layer (runs on AWS Academy Learner Lab):** CodeCommit push → EventBridge → Lambda orchestrator → EC2 build host via SSM (tests, hashes, D1, D2) → SNS approval → CodeDeploy rolling deploy to 2 EC2 behind an ALB, with `ValidateService` running D3, D5, D6 and automatic rollback on hook failure or CloudWatch alarm.

`PRD.md` is the source of truth for requirements (FR-xx, NFR-xx), acceptance criteria and decisions (ADR-xxx). If a task conflicts with the PRD, stop and ask.

## Your role

You work autonomously through the backlog in GitHub Issues, following the Working loop below. The human owner makes design decisions and approves the gated items listed under "Stop-and-ask gates"; everything else you build, test and merge yourself.

- `PRD.md` and the "Decisions already made" section below are your spec. Do not pick a design, threshold, library, model or tool that neither names. When a decision is missing, file it as a question on the issue (label `needs-info`), skip that issue and keep working on others.
- Do not do research tasks (choosing between approaches, surveying tools). Report the gap instead.
- Keep each PR scoped to one issue. No drive-by refactors.

## Decisions already made

- Python 3.11+, managed with `uv`. One `pyproject.toml` at the root. Top-level packages `gate`, `faults`, `analysis`, `orchestrator` (no `src/` layout). Dev tools: `pytest`, `ruff` (line length 100, target py311, rules E, F, I, B, UP). License MIT.
- llama.cpp built from source in `~/llama.cpp` (CPU build); binaries found through `LLAMA_CPP_BIN` (default `~/llama.cpp/build/bin`). Pin the llama.cpp commit in `serving/llama_cpp.lock`.
- Models, download sources, detector data (D2 corpus, D3 prompts, D5 panel and hidden pool), detector metrics, the D5 interval method, D6 and the F3 flags are fixed in `PRD.md` Section 6.2.1. Follow it exactly. Models download into `models/` (gitignored) by a make target; never committed.
- Serving config (FR-03) is one YAML file, `serving/config.yaml`, plus chat templates under `serving/templates/`.
- Gate and detector output is JSON with a stable, versioned schema.
- Offline AWS code (orchestrator Lambda, approval handler) is tested with `moto`; CloudFormation is checked with `cfn-lint`. No real AWS calls anywhere in tests.
- Long runs (calibration, fault matrix) run in `tmux`, write logs and results under `runs/<date>-<task>/`, and never block the loop: start them, then continue with other issues.

## Working loop

1. **Backlog.** The human creates the issues with `/to-tickets` (it is user-invoked only). Never create `T-NN` issues yourself. If no unblocked `ready-for-agent` issue exists, stop and report.
2. **Pick** the next `ready-for-agent` issue in the priority order below whose blockers are closed.
3. **Branch** `t-NN-short-name` from an up-to-date `main`.
4. **Build with `/tdd`:** turn each acceptance criterion into a failing test first (`tests/test_frNN_*.py`), then make it pass.
5. **Verify:** `make lint` and `make test` pass locally.
6. **PR:** title `T-NN: <summary>`, body with `Closes #n`, the acceptance criteria as a checklist with evidence (command plus output) for each.
7. **Merge:** when CI is green, squash-merge and delete the branch. If CI or a test fails, use `/diagnosing-bugs`. After three failed attempts on the same failure, comment on the issue, label it `needs-info`, close the PR as draft and move on.
8. **Repeat** from step 2. When your context is getting long, finish or park the current issue, write `docs/agents/handoff.md` (merged so far, the issue in progress and its branch, next issue, open questions, running tmux jobs), commit it to `main`, and stop. The human starts a new session that begins by reading that file.
9. **Stop** when no `ready-for-agent` issue is unblocked. Post a summary comment on the latest PR: what merged, what is waiting on the human and why.

Priority order for the off-AWS phase: T-03, T-04, T-30, T-05, T-11, T-06, T-08 (draft only, never merge), T-07, T-14, T-25, T-12, T-10, T-29, T-27. T-15 and T-16 start only after the human merges T-08.

## Stop-and-ask gates

Do not do these yourself. Label the issue `ready-for-human`, comment what is needed, and continue with other issues.

- Any AWS or Learner Lab action, and any command that calls AWS. You may write AWS code and templates; you may not run them against AWS.
- The pre-registration (T-08): you may draft it in a PR, but never merge it. The human merges it.
- Choosing or changing a detector threshold, a prompt budget, or an alpha.
- Adding a dependency, model, dataset or tool that `PRD.md` or this file does not name.
- Anything that spends money or deletes data outside the repo.

## Hard rules (never break these)

1. Never edit anything under `docs/prereg/` after the pre-registration commit exists.
2. Never change detector thresholds by hand. Thresholds come only from the calibration script output.
3. Never commit credentials, AWS keys, `.env` files, or Lab session tokens.
4. Never commit model weights or large artifacts (`*.gguf`, logits files). They live in `models/` and `artifacts/` (gitignored) or S3.
5. Keep detectors deterministic given a seed. Every randomized step takes an explicit `--seed`.
6. Run `make test` and `make lint` before proposing a diff. Both must pass.
7. Tag every commit message and PR title with the task id, e.g. `T-04: add SHA-256 manifest check`.
8. Never call AWS or create cloud resources (see Stop-and-ask gates).
9. Never force-push to `main` and never rewrite history on `main`.

## Environment

- Development runs in WSL2 Ubuntu. The repo lives at `~/projects/driftgate`, not under `/mnt/c`.
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

- One branch and one PR per issue: `t-NN-short-name`.
- Tests are keyed by requirement id in the file name (`tests/test_fr06_logprob_agreement.py`). Each acceptance criterion in a task maps to at least one test or a documented manual check.
- Gate and detector output is JSON with a stable schema; never print-only.
- Paths, ports, model names and thresholds come from config files, not hard-coded values.
- Commit messages: `T-NN: <imperative summary>`.

## Agent skills

### Issue tracker

Issues are tracked in GitHub Issues on sankalp-rajeev/DriftGate through the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `GLOSSARY.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
