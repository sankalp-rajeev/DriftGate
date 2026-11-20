---
date: 2026-11-19
faults_sha256: 81f02e436c4842a30bbca7c7780cf5748c61a83ed6f9e1f32372ba1f67a7ff01
baseline:
  model_sha256: d7efb072e7724d25048a4fda0a3e10b04bdef5d06b1403a1c93bd9f1240a63c8
  config_hash: 5c1277b1db8a99d27b529ae10370372e4da22ad6134478f18dcd8aae19d75d49
---

# Thread-count deviation, 19 November 2026

Owner-authorized change before study data collection (T-15, FR-03, FR-10, FR-11).
The frozen pre-registration under `docs/prereg/` is unchanged.

`serving/config.yaml` replaces `threads: -1` with
`threads: {local-Sankalps-Razer: 8, t3.large: 2, default: 2}`. The launcher selects the entry
for the instance-type component of the reference key, falling back to `default`. The config
hash covers the entire map, before selection. F9 changes this machine's serving thread count
to 1 (8 to 1 on the laptop, 2 to 1 on `t3.large` and the default). D2 remains at `--threads 2`
for both reference logits and candidates on every machine.

The owner's llama-bench measurements on this laptop show that automatic thread selection
collapses throughput on its hybrid CPU:

| Threads | Generation throughput (tokens/s) |
| --- | --- |
| 8 | 42 |
| 24 | 1.45 |
| 32 | Worse than 24; exact value not recorded |

The YAML front matter records the new effective serving config hash, the SHA-256 of
`faults/faults.yaml`, and the approved baseline model hash. These pins replace the corresponding
pre-registration pins. The study runner reads the latest dated deviation on `origin/main`,
requires its commit in the current checkout, and checks the fault file and baseline against it.
Before this file is merged, the new pins are not accepted for a study run.

No study data had been collected when this deviation was authorized. The interrupted timing
run never finished and produced no completed measurements. `runs/2026-11-19-study/` is discarded.
The reference measurement (`make reference SEED=0`) and D2 base logits are regenerated under
the new reference key before the owner starts the study. Cached fault bundles are rebuilt
from the updated config and fault definitions.

All other pre-registered detector definitions, data, budgets, seeds, repetition rules,
calibration rules and analyses remain unchanged. The study is not started by this fix.
