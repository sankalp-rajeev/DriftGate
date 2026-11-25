# Learner Lab session 2, 24 November 2026

Copied from `~/driftgate-lab-runs/session2-20261124-1625/`. The unattended run
ended with `rc=0` at 18:35:18 America/New_York after session teardown. Log progress
times use that timezone; AWS timestamps retain UTC. The deployed revision was
`6c6732ba1d0e11a2572ae7330224602bffe253bd` (PR #72).

| File | Evidence |
| --- | --- |
| [run.log](run.log) | Bootstrap READY, reference regeneration, three successful deploys, teardown and final return code. |
| [summary.txt](summary.txt) | Six L1 A/A gate runs: three deployments across two serving hosts. |
| [reference.log](reference.log) | Reference measurement and D2 logits regenerated with the generic AVX-512 build and uploaded to the reference key. |
| [cache.txt](cache.txt) | Shared binary archive at `bin/llama.cpp/8c16babba5d4c66e28a19d86e572e4b04f522d52/al2023-x86_64-avx512.tar.gz` (11,470,231 bytes). |
| [alb-health.txt](alb-health.txt) | ALB `/health` returned HTTP 200 with `{"status":"ok"}`. |
| `deploy-*.json` | CodeDeploy deployment records, each `Succeeded` on the same revision. |
| `deploy-*/<instance-id>/verdict.json` | Original report-mode D3/D5/D6 verdicts; also used as offline regression fixtures. |

The deployments were `d-FUPLUK70N`, `d-ZZQ4G970N` and **`d-A6O7XR80N`**
(letter O after 6, as recorded by AWS). Each ran on `i-094106129f457e2cf`
and `i-0f50eb6bf39419347`.

All six runs reported D3 agreement **1.0**, approximate KL **0.0**, and D5
reference/candidate accuracy **0.609375**, with zero paired mismatches.
ITL ratios ranged from **0.906 to 0.964** and TTFT ratios from **0.988 to 0.998**.
Mean L1 measurement time was **622.27 s (10.37 min)**: D3 **113.48 s** plus
D5 **508.80 s**, with per-instance totals from **618.10 to 627.56 s**. This is
about 3.7% above NFR-01's 600-second target, within the 900-second hook limit.
These are detector times, not complete rollout times.

The six original verdicts remain in report mode. Offline enforcement checks use
the [Lab threshold profile](../../gate/thresholds.json); no historical verdict
has been rewritten. See the [threshold deviation](../../docs/deviations/2026-11-24-t3large-thresholds.md)
for the evidence limits and the required Phase 4 F9 check.

The binary cache was populated before the first deployment, and later deploys reused
the installed binaries. The successful first deployment is consistent with serving
cache hits; this archive does not contain individual AfterInstall download logs.
It also contains D2 reference generation, but no D2 candidate comparison reports.
The owner reports cache reuse; neither a live rollback nor alarm failure was tested here.

The 14 source evidence files were copied byte-for-byte. `bundle/`, model weights,
raw reference measurements and logits are excluded. `run.log` lists all three
instances as terminated, with only the persistent storage stack remaining.
