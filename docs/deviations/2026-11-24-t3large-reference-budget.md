---
date: 2026-11-24
faults_sha256: 81f02e436c4842a30bbca7c7780cf5748c61a83ed6f9e1f32372ba1f67a7ff01
baseline:
  model_sha256: d7efb072e7724d25048a4fda0a3e10b04bdef5d06b1403a1c93bd9f1240a63c8
  config_hash: 5c1277b1db8a99d27b529ae10370372e4da22ad6134478f18dcd8aae19d75d49
---

# t3.large reference budget, 24 November 2026

The Lab measurement on t3.large at `--threads 2` was about 7.8 seconds per D3
prompt or D5 item. The reference run at budget 256 exceeded 60 minutes, and
SSM's default 3,600-second `executionTimeout` killed it before it wrote output.
The orchestrator now defaults to 7,200 seconds for both reference and build
commands.

| ValidateService level | D3 prompts | D5 items | Estimated time per instance |
| --- | ---: | ---: | ---: |
| L1 | 16 | 64 | 10m 24s |
| L2 | 32 | 128 | 20m 48s |
| L3 | 64 | 256 | 41m 36s |

By time, L1 is the only usable candidate for the
[pre-registered smallest eligible level rule](../prereg/preregistration.md#5-budget-levels).
L2 and L3 exceed both the 10-minute NFR-01 target and the 15-minute hook limit.
The L1 estimate is above the target and within the hook limit. The measured
result is below. The held-out false-alarm requirement still applies.

On t3.large, `gate reference` now defaults to 64 prompts and 64 items, which
covers L1. The default on other instance types is 256 for each detector, and
`--budget` still overrides either default. ValidateService returns exit 2
before measuring the candidate if the stored reference has too few results
for the requested level. The laptop study retains all three levels.

## Measured session 1 results

The t3.large L1 gate took **636 s (10.6 min)**: 116 s for D3 and 520 s for D5.
That is **6% over NFR-01's 10-minute target**. D5 accounted for about **82%**
of the measured time. The separate reference run at budget 64 took **1,005 s**.
The budget levels remain as pre-registered.

See the [session 1 evidence](../lab-constraints-evidence.md#cross-instance-determinism-sample-1)
for the cross-instance comparison and the D6 denominator used in that run.

## Measured session 2 results

The [six L1 A/A verdicts](../../results/2026-11-24-lab-session2/README.md)
from three identical deployments on two t3.large hosts averaged **622.27 s
(10.37 min)**: **113.48 s D3 + 508.80 s D5**. Per-instance totals ranged
from **618.10 to 627.56 s**. D5 accounted for about **82%** of detector time.
These are measurement times, not the complete two-instance rolling deployment.

The mean is about **3.7% above NFR-01's 600-second target**, within the
900-second ValidateService limit. The regenerated budget-64 reference
measurement completed after **993 s**; the full reference job, including D2
logit generation, finished after **1,115 s**. These runs used the shared
generic AVX-512 build and the matching L1 reference prefix for D6.

The [Lab threshold deviation](2026-11-24-t3large-thresholds.md) authorizes
L1 enforcement with the existing laptop latency rule; it does not claim
that six Lab observations satisfy the original max-of-20 calibration rule.
