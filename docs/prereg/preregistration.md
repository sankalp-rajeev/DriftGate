# Pre-registration (FR-11)

**Status: DRAFT for owner review (T-08). Not frozen until the owner merges this PR.** After the
merge commit, nothing under `docs/prereg/` may be edited (AGENTS.md hard rule 1). Items marked
**[OWNER DECISION]** are not defined in `PRD.md` and must be filled in by the owner before merge.
The draft never chooses a threshold value, alpha or prompt budget (AGENTS.md Stop-and-ask gates).

## 1. What is frozen

| Item | Value |
| --- | --- |
| Reference model | `qwen2.5-1.5b-instruct-q8_0.gguf` (PRD §6.2.1); SHA-256 in `serving/manifest.json` |
| llama.cpp build | commit `8c16babba5d4c66e28a19d86e572e4b04f522d52` (`serving/llama_cpp.lock`) |
| Reference serving config | `serving/config.yaml` at the commit that precedes the first fault run; ctx_size 4096, parallel 1, seed 0 |
| Detector data | SHA-256 of each file in `gate/data/manifest.json` (below) |
| Detector definitions | PRD §6.2.1: D1, D2 (`--threads 2` always), D3, D5, D6 |
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

Exact diffs are produced by `make fault F=<id>` from `faults/faults.yaml`. Parameters for F6 to F9
not yet fixed in `faults/faults.yaml`: **[OWNER DECISION]**.

## 3. Detectors and statistics

- D1 template lint (ggufdoctor and rendered-template diff): any failure is a fail.
- D2: mean KLD, p99 KLD, same-top-token %, `llama-perplexity -c 512 --threads 2`.
- D3: agreement share (mean) and per-prompt KL (mean and p95), as defined in PRD §6.2.1.
- D5: paired per-item accuracy delta, 95% paired bootstrap interval (10,000 resamples, seed 0,
  percentile), exact McNemar p-value reported alongside.
- D6: p95 TTFT and p95 inter-token latency, thresholds expressed as a **ratio to the reference
  p95** measured by `gate reference` on the same instance type.
- D4: **[OWNER DECISION]** include or drop; if kept, the alpha.

## 4. Thresholds rule

Thresholds come only from the calibration script output (AGENTS.md hard rule 2); no value is
written here or edited by hand.

- Calibration data: at least 20 A/A runs per detector (FR-12), reference config, same instance type.
- Output: `gate/thresholds.json` (schema `driftgate.thresholds/2`), with the FPR estimate and its
  interval reported.
- Statistic-to-threshold rule (which A/A quantile or bound, and any margin): **[OWNER DECISION]**.
- Target: false-positive rate of 5% or less per detector on held-out A/A runs.
- Until `gate/thresholds.json` exists the gate runs in report mode.

## 5. Prompt budget rule

Budgets are the first N records of `d3_prompts.jsonl` and of `d5_panel.jsonl`, with N in {16, 64,
256}. The ValidateService budget is the smallest N at which the calibrated detectors hold FPR of
5% or less **and** the gate fits NFR-01 (10 minutes or less on t3.large). 16 is provisional in
report mode (PRD §6.8). Power is measured for every fault at all three budgets (FR-13).

## 6. Analysis plan

- Calibration and matrix scripts live in `analysis/`; every result row records commit, config hash
  and seed (NFR-07).
- Fault matrix: detection rate, interval and wall-clock per fault, detector and budget (FR-13).
- RQ1: detectors versus fault classes at FPR 5% or less. RQ2: rank correlation of KL against
  accuracy drop over F1 to F3 (correlation measure **[OWNER DECISION]**). RQ3: share of bitwise-identical greedy
  logprobs.
- F5 evasion (FR-14): public panel versus hidden pool, detection rate for both.
- **Gap:** the calibration and matrix scripts (T-15) do not exist yet. FR-11 requires the analysis
  script in the pre-registration commit. **[OWNER DECISION]**: land the scripts in this PR before
  merge, or accept this plan as the registered analysis and add the scripts afterward.

## 7. Deviations

Any change after the merge must be recorded as a dated deviation in a new file outside
`docs/prereg/`, with the reason.
