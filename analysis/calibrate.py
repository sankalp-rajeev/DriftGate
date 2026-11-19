"""A/A threshold calibration (FR-12, pre-registered rule in PRD Section 6.8).

Input: a directory of A/A runs from `python -m faults.aa` at budget 256, files
`run-NN.measurement.json` (a run at budget 256 covers 16 and 64, which are prefixes of the same
files), plus the reference measurement. Runs 0..19 calibrate; runs 20..29 are held out.

Rule, per statistic and per budget:
  * threshold = the worst A/A value among the calibration runs, no margin. "Worst" is the maximum
    of the oriented statistic (upper-type metrics as they are; agreement-type metrics as
    1 - value, so the threshold is the minimum agreement).
  * fail means strictly above the threshold. By exchangeability the per-statistic false-alarm
    rate is at most 1/(n+1) = 1/21, about 4.8%.
  * An upper threshold is never negative: a negative worst statistic (the candidate beat the
    reference in every A/A run) is recorded as 0. If every A/A value is zero, the threshold is
    zero and any change fails; that is the RQ3 result.
  * The held-out runs estimate the real rate: the share of held-out runs where a detector (any
    of its statistics) or the gate (any detector) exceeds its threshold, with a Clopper-Pearson
    95% interval.

Outputs (in --out-dir): thresholds-<budget>.json (schema driftgate.thresholds/2, never edited by
hand) and calibration.json (thresholds, held-out false-alarm rates with intervals, wall-clock).
The ValidateService budget is the smallest budget whose held-out false-alarm rate is at most
`--target-fpr` for every detector and whose gate wall-clock is within `--wall-clock-limit-s`.
`--write-thresholds` installs that budget's file as gate/thresholds.json.

  python -m analysis.calibrate --runs runs/2026-11-21-aa --reference <measurement.json> \\
      --out-dir runs/2026-11-21-aa/calibration
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import shutil
import sys
from pathlib import Path

from gate import cli
from gate.score import THRESHOLDS_SCHEMA

BUDGETS = (16, 64, 256)
N_CALIBRATION = 20
N_HELD_OUT = 10
# (detector, metric inside the detector's verdict block, kind)
STATISTICS = (
    ("D3", "mean_agreement_share", "agreement"),
    ("D3", "mean_approx_kl", "upper"),
    ("D3", "p95_approx_kl", "upper"),
    ("D5", "accuracy_drop", "upper"),
    ("D6", "ratio.p95_ttft_ms", "upper"),
    ("D6", "ratio.p95_itl_ms", "upper"),
)


def at_budget(m: dict, budget: int) -> dict:
    """The measurement restricted to its first `budget` D3 prompts and D5 items."""
    out = copy.copy(m)
    out["budget"] = budget
    out["d3"] = m["d3"][:budget]
    out["d5"] = m["d5"][:budget]
    return out


def _lookup(obj: dict, dotted: str):
    for key in dotted.split("."):
        obj = obj[key]
    return obj


def statistics(reference: dict, candidate: dict, budget: int) -> dict:
    """{(detector, metric): value} for one run at one budget."""
    dets = cli.detectors(at_budget(reference, budget), at_budget(candidate, budget))
    return {(d, m): _lookup(dets[d], m) for d, m, _ in STATISTICS}


def oriented(kind: str, value: float) -> float:
    return 1 - value if kind == "agreement" else value


def threshold_value(kind: str, values: list[float]) -> float:
    worst = max(oriented(kind, v) for v in values)
    if kind == "upper":
        return max(worst, 0.0)
    return 1 - worst


def exceeds(kind: str, value: float, threshold: float) -> bool:
    return oriented(kind, value) > oriented(kind, threshold)


def clopper_pearson(k: int, n: int, level: float = 0.95) -> tuple[float, float]:
    """Exact binomial interval for k events in n trials (bisection on the binomial cdf)."""
    alpha = 1 - level

    def cdf(x: int, p: float) -> float:  # P(X <= x)
        return sum(math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(x + 1))

    def solve(f) -> float:
        lo, hi = 0.0, 1.0
        for _ in range(60):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if f(mid) else (lo, mid)
        return (lo + hi) / 2

    lower = 0.0 if k == 0 else solve(lambda p: 1 - cdf(k - 1, p) < alpha / 2)
    upper = 1.0 if k == n else solve(lambda p: cdf(k, p) > alpha / 2)
    return lower, upper


def gate_seconds(m: dict, budget: int) -> float | None:
    """Generation time of the first `budget` D3 prompts, from llama-server timings."""
    try:
        return sum(
            (r["timings"]["prompt_ms"] + r["timings"]["predicted_ms"]) / 1000
            for r in m["d3"][:budget]
        )
    except KeyError:
        return None


def calibrate(
    reference: dict,
    runs: list[dict],
    budgets=BUDGETS,
    n_calibration: int = N_CALIBRATION,
    n_held_out: int = N_HELD_OUT,
) -> dict:
    if len(runs) < n_calibration + n_held_out:
        raise ValueError(f"need {n_calibration + n_held_out} A/A runs, found {len(runs)}")
    cal, held = runs[:n_calibration], runs[n_calibration : n_calibration + n_held_out]
    result = {"n_calibration": len(cal), "n_held_out": len(held), "budgets": {}}
    for b in budgets:
        stats_cal = [statistics(reference, m, b) for m in cal]
        stats_held = [statistics(reference, m, b) for m in held]
        rules = []
        for det, metric, kind in STATISTICS:
            value = threshold_value(kind, [s[(det, metric)] for s in stats_cal])
            rules.append({"detector": det, "metric": metric, "kind": kind, "value": value})
        by_det: dict[str, list[dict]] = {}
        for r in rules:
            by_det.setdefault(r["detector"], []).append(r)
        fa = {d: 0 for d in by_det} | {"gate": 0}
        for s in stats_held:
            fired = {
                d: any(exceeds(r["kind"], s[(d, r["metric"])], r["value"]) for r in rs)
                for d, rs in by_det.items()
            }
            for d, hit in fired.items():
                fa[d] += hit
            fa["gate"] += any(fired.values())
        n = len(held)
        secs = [gate_seconds(m, b) for m in runs]
        secs = [x for x in secs if x is not None]
        result["budgets"][str(b)] = {
            "rules": rules,
            "held_out_false_alarms": {
                d: {
                    "k": k,
                    "n": n,
                    "rate": k / n,
                    "ci95": list(clopper_pearson(k, n)),
                }
                for d, k in fa.items()
            },
            "per_statistic_bound": 1 / (len(cal) + 1),
            "gate_seconds_mean": sum(secs) / len(secs) if secs else None,
        }
    return result


def select_budget(result: dict, target_fpr: float, wall_clock_limit_s: float) -> int | None:
    """Smallest budget with every detector's held-out rate <= target and wall-clock in limit."""
    for b in sorted(int(x) for x in result["budgets"]):
        info = result["budgets"][str(b)]
        rates = [v["rate"] for d, v in info["held_out_false_alarms"].items() if d != "gate"]
        secs = info["gate_seconds_mean"]
        if all(r <= target_fpr for r in rates) and (secs is None or secs <= wall_clock_limit_s):
            return b
    return None


def load_runs(directory: Path) -> list[dict]:
    files = sorted(directory.glob("run-*.measurement.json"))
    return [json.loads(f.read_text()) for f in files]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="analysis.calibrate", description=__doc__.splitlines()[0])
    p.add_argument("--runs", type=Path, required=True, help="directory of A/A run files")
    p.add_argument("--reference", type=Path, required=True, help="reference measurement.json")
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--target-fpr", type=float, default=0.05)
    p.add_argument("--wall-clock-limit-s", type=float, default=600.0, help="NFR-01 (10 minutes)")
    p.add_argument("--write-thresholds", type=Path, nargs="?", const=Path("gate/thresholds.json"))
    args = p.parse_args(argv)
    reference = json.loads(args.reference.read_text())
    result = calibrate(reference, load_runs(args.runs))
    result["target_fpr"] = args.target_fpr
    result["wall_clock_limit_s"] = args.wall_clock_limit_s
    result["selected_budget"] = select_budget(result, args.target_fpr, args.wall_clock_limit_s)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for b, info in result["budgets"].items():
        doc = {"schema": THRESHOLDS_SCHEMA, "rules": info["rules"]}
        (args.out_dir / f"thresholds-{b}.json").write_text(json.dumps(doc, indent=2) + "\n")
    (args.out_dir / "calibration.json").write_text(json.dumps(result, indent=2) + "\n")
    if args.write_thresholds:
        if result["selected_budget"] is None:
            print("no budget meets the target; thresholds not installed", file=sys.stderr)
            return 1
        src = args.out_dir / f"thresholds-{result['selected_budget']}.json"
        shutil.copy(src, args.write_thresholds)
    print(json.dumps({"selected_budget": result["selected_budget"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
