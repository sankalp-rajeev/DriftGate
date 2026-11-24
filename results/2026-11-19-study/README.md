# Laptop study, 19–20 November 2026

This archive contains the compact outputs of `runs/2026-11-19-study/`. See the
[study results](../../docs/results.md) for the interpretation, provenance and deviations.

| File | Contents |
| --- | --- |
| `matrix.csv` | Detections, exact 95% intervals and mean D3 times by fault, detector and budget. Includes the [corrected F8 runs](../../docs/results/f8-repair.json). |
| `calibration/calibration.json` | Rules fitted on 20 A/A runs and false-alarm estimates from 10 held-out runs at each budget. |
| `calibration/thresholds-L{1,2,3}.json` | Saved laptop rules used to score this study. Lab thresholds will come from the separate t3.large calibration in Phase 2. |
| `determinism.json` | Restart determinism check that set the fault repetitions. |
| `timing.json` | Detector and startup timings from the separate L3 timing run. |
| `rq2.json`, `rq2-points.json` | Original RQ2 analysis and input coordinates. The original F3 D2 result omitted the KV flags. See the [flagged F3 run](../../docs/results/f3-kv.json) for the follow-up. |

Raw measurements, model weights and reference logits remain in local study or artifact storage.
