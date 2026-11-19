"""A/A threshold calibration (FR-12, pre-registered rule in PRD Section 6.8).

Input: a directory of A/A runs from `python -m faults.aa --level L3 --d2`, files
`run-NN.measurement.json` and `run-NN.d2.json`, plus the reference measurement. One run at L3
(D3 64 + D5 256) covers L2 (32 + 128) and L1 (16 + 64): levels are prefixes of the same files.
Runs 0..19 calibrate; runs 20..29 are held out.

One gating statistic per detector (everything else is reported, never gated):
D2 mean KLD; D3 1 - mean agreement share; D5 accuracy drop (reference - candidate); D6 p95
inter-token latency ratio to the reference. D1 is pass/fail.

Rule, per detector and per level:
  * threshold = the worst A/A value among the calibration runs, no margin; fail means strictly
    above it. By exchangeability the per-detector false-alarm rate is at most 1/(n+1) = 1/21.
  * An upper threshold is never negative: a negative worst value is recorded as 0. If every A/A
    value is zero the threshold is zero and any change fails (the RQ3 result).
  * The held-out runs count false alarms per detector. The gate-level rate (any detector firing)
    is reported with a Clopper-Pearson 95% interval; no bound is claimed for it.
  * A level is eligible unless a detector has 2 or more false alarms in the held-out runs. The
    ValidateService level is the smallest eligible one. NFR-01 is checked separately, with one
    timed gate run at that level on a t3.large in the Lab, not with timings from these runs.

Outputs (in --out-dir): thresholds-<level>.json (schema driftgate.thresholds/2, never edited by
hand) and calibration.json. `--write-thresholds` installs the selected level's file as
gate/thresholds.json.

  python -m analysis.calibrate --runs runs/2026-11-21-study/aa --reference <measurement.json> \\
      --out-dir runs/2026-11-21-study/calibration
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

# Budget levels (PRD Section 6.8; mirrored in gate/config.yaml `levels`): (D3 prompts, D5 items).
LEVELS = {"L1": (16, 64), "L2": (32, 128), "L3": (64, 256)}
N_CALIBRATION = 20
N_HELD_OUT = 10
MAX_FALSE_ALARMS = 1  # a level is eligible unless a detector has 2 or more
# Gating statistics: (detector, metric, kind). One per detector.
STATISTICS = (
    ("D2", "mean_kld", "upper"),
    ("D3", "mean_agreement_share", "agreement"),
    ("D5", "accuracy_drop", "upper"),
    ("D6", "ratio.p95_itl_ms", "upper"),
)
# Reported, never gated.
REPORTED = (
    ("D2", "p99_kld"),
    ("D2", "same_top_p_pct"),
    ("D3", "mean_approx_kl"),
    ("D3", "p95_approx_kl"),
    ("D5", "delta_accuracy"),
    ("D6", "ratio.p95_ttft_ms"),
)


def at_budget(m: dict, level: str) -> dict:
    """The measurement restricted to the first D3 prompts and D5 items of `level`."""
    n3, n5 = LEVELS[level]
    out = copy.copy(m)
    out["budget"], out["d5_budget"] = n3, n5
    out["d3"] = m["d3"][:n3]
    out["d5"] = m["d5"][:n5]
    return out


def _lookup(obj: dict, dotted: str):
    for key in dotted.split("."):
        obj = obj[key]
    return obj


def statistics(reference: dict, candidate: dict, level: str, reported: bool = False) -> dict:
    """{(detector, metric): value} for one run at one level.

    D2 appears only for runs that have D2 results (`candidate["d2"]`, the metrics of
    `gate.d2 run`); D3, D5 and D6 only when the run measured D3.
    """
    out: dict = {}
    wanted = {(d, m) for d, m, _ in STATISTICS} | (set(REPORTED) if reported else set())
    if candidate.get("d3"):
        dets = cli.detectors(at_budget(reference, level), at_budget(candidate, level))
        for d, m in wanted:
            if d != "D2":
                out[(d, m)] = _lookup(dets[d], m)
    if candidate.get("d2"):
        for d, m in wanted:
            if d == "D2" and m in candidate["d2"]:
                out[(d, m)] = candidate["d2"][m]
    return out


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


def rules_by_detector(rules: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rules:
        out.setdefault(r["detector"], []).append(r)
    return out


def fired(stats: dict, rules: list[dict], d1_ok: bool | None = None) -> dict[str, bool | None]:
    """{detector: fires?}; None when the run has no result for that detector."""
    out: dict[str, bool | None] = {}
    for det, rs in rules_by_detector(rules).items():
        if all((det, r["metric"]) in stats for r in rs):
            out[det] = any(exceeds(r["kind"], stats[(det, r["metric"])], r["value"]) for r in rs)
        else:
            out[det] = None
    if d1_ok is not None:
        out["D1"] = not d1_ok
    return out


def calibrate(
    reference: dict,
    runs: list[dict],
    levels=tuple(LEVELS),
    n_calibration: int = N_CALIBRATION,
    n_held_out: int = N_HELD_OUT,
) -> dict:
    if len(runs) < n_calibration + n_held_out:
        raise ValueError(f"need {n_calibration + n_held_out} A/A runs, found {len(runs)}")
    cal, held = runs[:n_calibration], runs[n_calibration : n_calibration + n_held_out]
    if any(not m.get("d2") for m in cal + held):
        raise ValueError("every A/A run needs D2 results (run faults.aa with --d2)")
    result = {"n_calibration": len(cal), "n_held_out": len(held), "levels": {}}
    for lv in levels:
        stats_cal = [statistics(reference, m, lv, reported=True) for m in cal]
        stats_held = [statistics(reference, m, lv) for m in held]
        rules = []
        for det, metric, kind in STATISTICS:
            value = threshold_value(kind, [s[(det, metric)] for s in stats_cal])
            rules.append({"detector": det, "metric": metric, "kind": kind, "value": value})
        reported = {
            f"{d}.{m}": {
                "min": min(s[(d, m)] for s in stats_cal),
                "max": max(s[(d, m)] for s in stats_cal),
            }
            for d, m in REPORTED
        }
        dets = list(rules_by_detector(rules))
        counts = {d: 0 for d in dets} | {"gate": 0}
        for s, m in zip(stats_held, held, strict=True):
            hits = fired(s, rules, m.get("d1_ok"))
            for d, hit in hits.items():
                counts[d] = counts.get(d, 0) + bool(hit)
            counts["gate"] += any(hits.values())
        n = len(held)
        result["levels"][lv] = {
            "d3_prompts": LEVELS[lv][0],
            "d5_items": LEVELS[lv][1],
            "rules": rules,
            "reported_aa_range": reported,
            "held_out_false_alarms": {
                d: {"k": k, "n": n, "rate": k / n, "ci95": list(clopper_pearson(k, n))}
                for d, k in counts.items()
            },
            "per_detector_bound": 1 / (len(cal) + 1),
            "gate_bound": None,  # no bound is claimed for the gate-level rate
        }
    return result


def select_level(result: dict) -> str | None:
    """Smallest level where no detector has 2 or more held-out false alarms."""
    for lv in sorted(result["levels"], key=lambda x: LEVELS[x]):
        fa = result["levels"][lv]["held_out_false_alarms"]
        if all(v["k"] <= MAX_FALSE_ALARMS for d, v in fa.items() if d != "gate"):
            return lv
    return None


def load_runs(directory: Path) -> list[dict]:
    """Run measurements in order, with `d2` (metrics) and `d1_ok` attached when present."""
    runs = []
    for f in sorted(directory.glob("run-*.measurement.json")):
        m = json.loads(f.read_text())
        stem = f.name.removesuffix(".measurement.json")
        d2_file, d1_file = f.with_name(f"{stem}.d2.json"), f.with_name(f"{stem}.d1.json")
        if d2_file.exists():
            m["d2"] = json.loads(d2_file.read_text())["metrics"]
        if d1_file.exists():
            m["d1_ok"] = json.loads(d1_file.read_text())["ok"]
        runs.append(m)
    return runs


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="analysis.calibrate", description=__doc__.splitlines()[0])
    p.add_argument("--runs", type=Path, required=True, help="directory of A/A run files")
    p.add_argument("--reference", type=Path, required=True, help="reference measurement.json")
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--write-thresholds", type=Path, nargs="?", const=Path("gate/thresholds.json"))
    args = p.parse_args(argv)
    reference = json.loads(args.reference.read_text())
    result = calibrate(reference, load_runs(args.runs))
    result["selected_level"] = select_level(result)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for lv, info in result["levels"].items():
        doc = {"schema": THRESHOLDS_SCHEMA, "rules": info["rules"]}
        (args.out_dir / f"thresholds-{lv}.json").write_text(json.dumps(doc, indent=2) + "\n")
    (args.out_dir / "calibration.json").write_text(json.dumps(result, indent=2) + "\n")
    if args.write_thresholds:
        if result["selected_level"] is None:
            print("no level is eligible; thresholds not installed", file=sys.stderr)
            return 1
        selected = args.out_dir / f"thresholds-{result['selected_level']}.json"
        shutil.copy(selected, args.write_thresholds)
    print(json.dumps({"selected_level": result["selected_level"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
