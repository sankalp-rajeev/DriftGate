# t3.large L1 thresholds, 24 November 2026

The owner authorizes installing [gate/thresholds.json](../../gate/thresholds.json)
for the Lab gate after [session 2](../../results/2026-11-24-lab-session2/README.md).
This is a deviation from a separate t3.large max-of-20 calibration under FR-12.
The pre-registration and the archived laptop study remain unchanged. The human
merges this change because the file's existence switches `mode: auto` to enforce.

| Detector | Gating statistic | Installed L1 threshold |
| --- | --- | ---: |
| D2 | Mean KLD | 0 |
| D3 | 1 minus mean agreement share | 0 |
| D5 | Accuracy drop (reference minus candidate) | 0 |
| D6 | Candidate/reference p95 ITL ratio | 1.37 |

The schema stores D3 as `mean_agreement_share`, kind `agreement`, value `1.0`:
that is the same zero threshold on 1 minus agreement. D2 runs on the build host;
ValidateService scores D3, D5 and D6. Each stage applies only its detector rules.
Any statistic strictly over its threshold fails with exit 1; equality passes.
TTFT and approximate KL are reported, never gated.

## Behavioral evidence

Six of six A/A gate runs on two t3.large serving hosts matched a reference
regenerated on the build host using the same cached generic AVX-512 build
(`GGML_NATIVE=OFF`, `GGML_AVX512=ON`, VNNI/VBMI/BF16 off). D3 agreement was
**1.000**, approximate KL **0**, and D5 accuracy **0.609375 vs 0.609375**
(rounded **0.609 vs 0.609**), with no paired mismatches.

Together with the **30/30 laptop A/A runs**, this supports the owner's zero
thresholds for D2/D3/D5. The saved [laptop L1 calibration](../../results/2026-11-19-study/calibration/thresholds-L1.json)
already gives these zero behavioral thresholds. Session 2's six live verdicts
measure D3/D5/D6, not D2: its [reference log](../../results/2026-11-24-lab-session2/reference.log)
proves D2 logits were regenerated, but contains no candidate D2 comparison.
The D2 zero threshold is carried from the laptop calibration, not inferred
from six unrecorded Lab D2 runs.

## Latency decision and limits

Only six Lab runs exist. A max-of-six rule would have an exchangeability bound
of **1/7 (about 14.3%)**, rather than the pre-registered max-of-20 bound of
1/21. These are three repeated deploys on the same two serving hosts, not six
independent host draws; serving hosts change between sessions. No 5% Lab
false-alarm guarantee, held-out Lab estimate, or bound on the combined gate
is claimed. The laptop and Lab counts are not pooled for a bound.

D6 therefore retains the owner's **1.37** L1 value from the laptop calibration,
rather than fitting to the faster session-2 hosts. The saved unrounded value
is **1.3720862440822872**; the installed **1.37** is the two-decimal value
explicitly authorized for the Lab. It is a calibration result from the
pre-registered rule, not a numeric threshold fixed in the pre-registration.

Observed Lab ITL ratios were **0.906–0.964**: approximately **0.91** on
`i-094106129f457e2cf` and **0.95** on `i-0f50eb6bf39419347`. TTFT ratios
were **0.988–0.998**. All six recorded verdicts pass the installed thresholds
when scored offline. The original deployments ran in report mode.

**Phase 4 must check F9 (threads 2 → 1) on t3.large and confirm that the p95
ITL ratio exceeds 1.37. If it does not, record the null result.** Do not
change the threshold to make F9 detectable.

## Timing and reproduction

The six L1 runs averaged **113.48 s D3 + 508.80 s D5 = 622.27 s**
(**10.37 min**, approximately 10.4 min). This exceeds NFR-01 by about 3.7%
and remains below the 15-minute hook limit. See the [timing deviation](2026-11-24-t3large-reference-budget.md#measured-session-2-results).

The threshold file was derived from the saved calibration output, with only
the owner-authorized two-decimal D6 representation applied:

```sh
uv run python - <<'PY'
import json
from pathlib import Path

source = Path("results/2026-11-19-study/calibration/thresholds-L1.json")
thresholds = json.loads(source.read_text())
for rule in thresholds["rules"]:
    if rule["detector"] == "D6":
        rule["value"] = round(rule["value"], 2)
Path("gate/thresholds.json").write_text(json.dumps(thresholds, indent=2) + "\n")
PY
```

`tests/test_fr18_hooks.py` scores the six original verdicts and runs
ValidateService against local HTTP responses consistent with each verdict's
aggregate accuracy and latency. Separate cases exceed each live detector's
threshold and require exit 1. `tests/test_fr05_d2.py` checks the build-stage
D2 CLI against zero and positive KLD using a local llama-perplexity stand-in.
