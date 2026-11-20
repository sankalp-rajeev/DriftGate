# Pre-registration (FR-11)

**Status: DRAFT for owner review (T-08). Not frozen until the owner merges this PR.** After the
merge commit, nothing under `docs/prereg/` may be edited (AGENTS.md hard rule 1). All rules below
are the owner's decisions of 19 November 2026 (PRD §6.8). This document sets no threshold value,
alpha or budget level: thresholds come only from the calibration script output.

## 1. What is frozen

| Item | Value |
| --- | --- |
| Reference model | `qwen2.5-1.5b-instruct-q8_0.gguf` (PRD §6.2.1); SHA-256 in `serving/manifest.json` |
| llama.cpp build | commit `8c16babba5d4c66e28a19d86e572e4b04f522d52` (`serving/llama_cpp.lock`) |
| Reference serving config | `serving/config.yaml` at the commit that precedes the first fault run (ctx_size 4096, parallel 1, seed 0), identified by the baseline in `gate/baseline.yaml`: model SHA-256 `d7efb072e7724d25048a4fda0a3e10b04bdef5d06b1403a1c93bd9f1240a63c8`, config hash `d0db861e0c31b4d0928cd73cceb36f81f2f32e63dec47dd78a656d1a405fc541` |
| Detector data | SHA-256 of each file in `gate/data/manifest.json` (below) |
| Detector definitions | PRD §6.2.1: D1, D2 (`--threads 2` always), D3, D5, D6 |
| Baseline | `gate/baseline.yaml` (model SHA-256 and config hash of the reference release); candidates are compared against its reference at `reference/<model sha>/<config hash>/<instance-type>/` |
| Fault definitions | `faults/faults.yaml`, SHA-256 `e2d4e308eccac976a431adfa1c75622794e2d62c7af63591cf8c8439c03635a8` |
| Seeds | The gate measurement seed is 0 (the reference's seed; decoding is greedy). Each repeated run has a run seed (1 to R) that drives hidden-pool sampling. Selection and bootstrap use seed 0 |

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

Exact diffs are produced by `make fault F=<id>` from `faults/faults.yaml` (SHA-256 above):

- F5: in the template's tools branch only, the sentence that tells the model to return function
  calls inside `<tool_call></tool_call>` tags is deleted. Every other branch is byte-identical.
  **Pre-registered expectation: D1 catches F5; D3 and D5 miss it on both the public panel and the
  hidden pool, because neither uses tools.**
- F6: default sampler temperature raised by 0.5 (0.8 to 1.3). The gate sends explicit greedy
  requests, so a **null result is pre-registered**: the gate pins sampling, and catching drift in
  sampler defaults needed D4, which is cut.
- F7: `ctx_size` 4096 to 256.
- F8: the latest llama.cpp release tag on the day T-15 starts; the exact tag is recorded in the
  run log and built as a separate llama.cpp checkout.
- F9: serving threads -1 (auto) to 1.
- Analysis-only quantization sweep for RQ2: Q8_0, Q6_K, Q5_K_M, Q4_K_M, Q3_K_M and Q2_K from the
  same Qwen repo, plus F3. It runs D2 and D5 only, once per model.

## 3. Detectors and gating statistics

One gating statistic per detector. Every other statistic is reported, never gated.

| Detector | Gating statistic | Reported only |
| --- | --- | --- |
| D1 | pass/fail (ggufdoctor and rendered-template diff) | |
| D2 | mean KLD (`llama-perplexity -c 512 --threads 2`) | p99 KLD, same-top-token % |
| D3 | 1 minus mean agreement share | mean and p95 approximate KL |
| D5 | accuracy drop (reference minus candidate) | paired delta, 95% bootstrap interval (10,000 resamples, seed 0, percentile), exact McNemar p |
| D6 | p95 inter-token latency, as a ratio to the reference p95 | p95 TTFT ratio |

D4 is cut (FR-07 is marked cut in the PRD). No alpha is used.

D2 runs only for A/A, F1, F2, F3, F8 and the quantization sweep. D1 runs for every fault.

## 4. Thresholds rule

Thresholds come only from the calibration script output (AGENTS.md hard rule 2); no value is
written here or edited by hand.

- Calibration: 20 A/A runs. Each is a full identical redeploy with llama-server restarted. One run
  at L3 covers L1 and L2 (section 5). D2 is part of every A/A run.
- Threshold, per detector and level = the maximum A/A value of its gating statistic, with no
  margin. Fail means strictly above the threshold. By exchangeability this bounds the false-alarm
  rate per detector at 1/21, about 4.8%.
- Ten more held-out A/A runs count false alarms per detector. The gate-level rate (any detector
  firing) is estimated on the same ten runs and reported with a Clopper-Pearson 95% interval; no
  bound is claimed for it.
- If every A/A value is zero, the threshold is zero, and that is reported as the RQ3 result. A
  negative maximum for an upper-type statistic is recorded as zero. A zero threshold gives a
  finite gate score (1e6) beyond it.
- Output: threshold files per level (schema `driftgate.thresholds/2`) and `calibration.json`,
  written by `analysis/calibrate.py`. D6 thresholds are ratios to the reference p95.
- Until `gate/thresholds.json` exists the gate runs in report mode.

## 5. Budget levels

| Level | D3 prompts | D5 items |
| --- | --- | --- |
| L1 | 16 | 64 |
| L2 | 32 | 128 |
| L3 | 64 | 256 |

Levels are prefixes of the frozen `d3_prompts.jsonl` and `d5_panel.jsonl`, and one run at L3
covers all levels. A level is eligible unless a detector has 2 or more false alarms in the 10
held-out A/A runs. The ValidateService level is the smallest eligible level (16 prompts and 16
items stay provisional in report mode). NFR-01 (10 minutes or less on t3.large) is checked with one
timed gate run at that level on a t3.large in the Lab, not with laptop timings. Power is measured
for every fault at all three levels (FR-13).

## 6. Repetitions and analysis plan

- Repetitions per fault: if all 20 calibration A/A runs give bitwise-identical D3 and D5 outputs,
  each fault runs 2 times (run seeds 1 and 2); otherwise 5 times (run seeds 1 to 5). Each run is a
  fresh redeploy. Hidden-pool items are sampled per run with that run's seed.
- Detection rate = detections out of runs completed, with a Clopper-Pearson 95% interval. If
  compute runs out, the runs completed are reported and F8 and F9 are cut first (PRD §17).
- The quantization sweep is one run per model.

Scripts (written and merged before this pre-registration, no runs; commit
`b5619eb56f16d1f998606f409b83fa028c057240`):

- `analysis/calibrate.py`: calibration, thresholds, held-out false-alarm rates with intervals,
  level selection.
- `analysis/matrix.py`: detection rate, interval and wall-clock per fault, detector and level
  (FR-13), as CSV.
- `analysis/rq2.py`: Spearman correlation with an exact permutation p-value.
- `analysis/study.py` (`make study`): the overnight runner. It refuses to start unless this
  pre-registration is merged into main and `faults/faults.yaml` matches the hash above.

Every result row records commit, config hash and seed (NFR-07).

- RQ1: detectors versus fault classes at FPR 5% or less, from the matrix.
- RQ2: Spearman rank correlation between D2 mean KL and the D5 accuracy drop, with an exact
  permutation p-value, over the quantization sweep plus F3.
- RQ3: share of bitwise-identical greedy outputs (A/A and F9).
- F5 evasion (FR-14): public panel versus hidden pool, detection rate for both.

## 7. Deviations

Any change after the merge must be recorded as a dated deviation in a new file outside
`docs/prereg/`, with the reason.
