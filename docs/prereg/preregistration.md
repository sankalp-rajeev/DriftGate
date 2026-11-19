# Pre-registration (FR-11)

**Status: DRAFT for owner review (T-08). Not frozen until the owner merges this PR.** After the
merge commit, nothing under `docs/prereg/` may be edited (AGENTS.md hard rule 1). All rules below
are the owner's decisions of 19 November 2026 (PRD §6.8). This document sets no threshold value,
alpha or prompt budget: thresholds come only from the calibration script output.

## 1. What is frozen

| Item | Value |
| --- | --- |
| Reference model | `qwen2.5-1.5b-instruct-q8_0.gguf` (PRD §6.2.1); SHA-256 in `serving/manifest.json` |
| llama.cpp build | commit `8c16babba5d4c66e28a19d86e572e4b04f522d52` (`serving/llama_cpp.lock`) |
| Reference serving config | `serving/config.yaml` at the commit that precedes the first fault run; ctx_size 4096, parallel 1, seed 0 |
| Detector data | SHA-256 of each file in `gate/data/manifest.json` (below) |
| Detector definitions | PRD §6.2.1: D1, D2 (`--threads 2` always), D3, D5, D6 |
| Baseline | `gate/baseline.yaml` (model SHA-256 and config hash of the reference release); candidates are compared against its reference at `reference/<model sha>/<config hash>/<instance-type>/` |
| Seeds | Every randomized step takes an explicit `--seed`; selection, bootstrap and A/A runs use seed 0 unless a run states another |

Data hashes (SHA-256):

- `d2_corpus.txt` 6c61f209b53a8f7e0ee25e22815b8472d1a29cf891f0a29bca35ae82cd285b75
- `d3_prompts.jsonl` 72ac638abbc4c635160c496feddfb06a4e194ef3b5e5a86be1c869b72e171dd0
- `d5_reference_scores.jsonl` a24d65db21892e0cb1f3f260ee31107a2ecf1cafc7440db76b03039c34b59f33
- `d5_panel.jsonl` 796b864cbf9b45481a9d0e4c6e8e239c9a90fc209b818d777b433153806dba4d
- `d5_hidden_pool.jsonl` e35e2d50ed8a34346f42d8caf9aab8b963983dcb78712317185b60166049b4c5

D5 selection facts: all 14,042 MMLU test questions scored on the reference; 1,399 have a
gold-letter probability in [0.25, 0.75]; panel 256, hidden pool 512.

## 2. Fault list

| ID | Change | Layer |
| --- | --- | --- |
| A/A | Identical redeploy | None |
| F1 | Weights Q8_0 to Q4_K_M | Artifact |
| F2 | Weights Q8_0 to Q2_K | Artifact |
| F3 | KV cache quantized at launch (`--cache-type-k q4_0 --cache-type-v q4_0 --flash-attn on`) | Serving |
| F4 | Edit to the default system prompt in the chat template | Serving |
| F5 | Tool-call branch of the template altered (evasion probe) | Serving |
| F6 | Sampler default changed in launch config | Serving |
| F7 | Context window cut below panel prompt length | Serving |
| F8 | llama.cpp build bump | Engine |
| F9 | Thread count changed (RQ3 control) | Engine |

Exact diffs are produced by `make fault F=<id>` from `faults/faults.yaml`:

- F6: default sampler temperature raised by 0.5 (0.8 to 1.3). The gate sends explicit greedy
  requests, so a **null result is pre-registered**: the gate pins sampling, and catching drift in
  sampler defaults needed D4, which is cut.
- F7: `ctx_size` 4096 to 256.
- F8: the latest llama.cpp release tag on the day T-15 starts; the exact tag is recorded in the
  run log.
- F9: serving threads 2 to 1.
- F5: the tool-call branch of the template, as defined in `faults/faults.yaml` before any F5 run.
- Analysis-only quantization sweep for RQ2: Q8_0, Q6_K, Q5_K_M, Q4_K_M, Q3_K_M and Q2_K from the
  same Qwen repo, plus F3.

## 3. Detectors and statistics

- D1 template lint (ggufdoctor and rendered-template diff): any failure is a fail.
- D2: mean KLD, p99 KLD, same-top-token %, `llama-perplexity -c 512 --threads 2`.
- D3: agreement share (mean) and per-prompt KL (mean and p95), as defined in PRD §6.2.1.
- D5: paired per-item accuracy delta, 95% paired bootstrap interval (10,000 resamples, seed 0,
  percentile), exact McNemar p-value reported alongside.
- D6: p95 TTFT and p95 inter-token latency, thresholds expressed as a **ratio to the reference
  p95** measured by `gate reference` on the same instance type.
- D4: cut (FR-07 is marked cut in the PRD, first item on the cut list). No alpha is used.

## 4. Thresholds rule

Thresholds come only from the calibration script output (AGENTS.md hard rule 2); no value is
written here or edited by hand.

- Calibration: 20 A/A runs per detector. Each is a full identical redeploy with llama-server
  restarted. One run at budget 256 covers 16 and 64, because budgets are prefixes of the same
  files.
- Threshold = the maximum A/A statistic at that budget, with no margin (for agreement-type
  metrics, the oriented statistic 1 minus agreement). Fail means strictly above the threshold.
  By exchangeability this bounds the false-alarm rate at 1/21, about 4.8%.
- Ten more held-out A/A runs estimate the actual rate, with a Clopper-Pearson 95% interval.
- If every A/A statistic is zero, the threshold is zero, and that is reported as the RQ3 result.
  A negative maximum for an upper-type statistic is recorded as zero.
- Output: threshold files per budget (schema `driftgate.thresholds/2`) and `calibration.json`,
  written by `analysis/calibrate.py`; D6 thresholds are ratios to the reference p95.
- Until `gate/thresholds.json` exists the gate runs in report mode.

## 5. Prompt budget rule

Budgets are the first N records of `d3_prompts.jsonl` and of `d5_panel.jsonl`, with N in {16, 64,
256}. The ValidateService budget is the smallest N at which the calibrated detectors hold FPR of
5% or less (held-out point estimate, per detector) **and** the gate fits NFR-01 (10 minutes or
less on t3.large). 16 is provisional in
report mode (PRD §6.8). Power is measured for every fault at all three budgets (FR-13).

## 6. Analysis plan

Scripts (written and merged before this pre-registration, no runs; commit `0938dff4824cbd62c1143749b695187d677b456f`):

- `analysis/calibrate.py`: calibration, thresholds, held-out false-alarm rate with interval,
  budget selection.
- `analysis/matrix.py`: detection rate, interval and wall-clock per fault, detector and budget
  (FR-13), as CSV.
- `analysis/rq2.py`: Spearman correlation with an exact permutation p-value.

Every result row records commit, config hash and seed (NFR-07).

- RQ1: detectors versus fault classes at FPR 5% or less, from the matrix.
- RQ2: Spearman rank correlation between D2 mean KL and the D5 accuracy drop, with an exact
  permutation p-value, over the quantization sweep plus F3.
- RQ3: share of bitwise-identical greedy logprobs (A/A and F9).
- F5 evasion (FR-14): public panel versus hidden pool, detection rate for both.

## 7. Deviations

Any change after the merge must be recorded as a dated deviation in a new file outside
`docs/prereg/`, with the reason.
