# Glossary

Meanings taken from `PRD.md`.

- **A/A run**: An identical redeploy with no change. Used to measure how often a detector fires when nothing changed, and to calibrate thresholds (at least 20 per detector, FR-12).
- **Fault (F1 to F9)**: A deliberate, reproducible change to the serving stack, expressed as a config or artifact diff: F1 Q8_0 to Q4_K_M weights, F2 Q8_0 to Q2_K, F3 KV cache quantized, F4 chat template edit, F5 tool-call template branch altered, F6 sampler default changed, F7 context window cut below panel prompt length, F8 llama.cpp build bump, F9 thread count changed (PRD §6.3).
- **Detector (D1 to D6)**: A check that compares a candidate serving stack with the reference: D1 chat-template lint, D2 artifact KL, D3 live top-k logprob agreement, D4 MMD two-sample test (optional), D5 exact-match panel, D6 latency SLO (PRD §6.2).
- **Gate verdict**: The gate's JSON output for a candidate: detector results and the ship or block decision. Stable, versioned schema.
- **Bundle**: The deployable package of model artifacts and serving config produced by the build stage or the fault injector (`make fault F=F4`), stored in S3.
- **Manifest**: The file pinning every model artifact by SHA-256. The build fails if any artifact hash differs from it (FR-02).
- **Run record**: The JSON record of one pipeline run, kept in S3 by the Lambda orchestrator, with every stage's timestamps, outcome and the approval decision (FR-15, FR-27).
- **Panel**: The D5 set of "sometimes right" exact-match items: a public panel of 256 MMLU items and a hidden pool of up to 512 for the evasion test (FR-14, §6.2.1).
- **Prompt budget**: The number of prompts a detector uses per run: 16, 64 or 256 (FR-13).
- **False-alarm rate (FPR)**: The share of A/A runs in which a detector fires. Target 5% or less per gate detector (NFR-02).
- **Pre-registration**: The commit, made before any fault run, that fixes the thresholds rule, fault list and analysis script (FR-11). Files under `docs/prereg/` are frozen after it.
- **ValidateService**: The CodeDeploy lifecycle hook that runs D3, D5 and D6 (and D4 if kept) against a freshly deployed instance and exits non-zero on gate failure, triggering rollback (FR-18).
