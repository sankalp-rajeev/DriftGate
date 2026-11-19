"""Fault matrix (FR-13): detection rate, interval and wall-clock per fault, detector and budget.

Input: `--faults` directories, one per fault (name = directory name, e.g. runs/.../F4), each
holding `run-NN.measurement.json` files from `python -m faults.aa --bundle <fault bundle>` at
budget 256 (budgets 16 and 64 are prefixes). `--thresholds-dir` is the output of
`analysis.calibrate` (thresholds-<budget>.json, produced by the pre-registered rule). A run is a
detection for a detector when any of that detector's statistics exceeds its threshold; the `gate`
row counts runs where any detector fires. The interval is Clopper-Pearson 95%.

  python -m analysis.matrix --reference <measurement.json> --thresholds-dir <dir> \\
      --faults runs/2026-11-24-F1 runs/2026-11-24-F4 --out runs/2026-11-24/matrix.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from analysis.calibrate import (
    BUDGETS,
    clopper_pearson,
    exceeds,
    gate_seconds,
    load_runs,
    statistics,
)

COLUMNS = ["fault", "budget", "detector", "runs", "detections", "rate", "ci95_low", "ci95_high"]
COLUMNS += ["gate_seconds_mean"]


def detections(reference: dict, runs: list[dict], rules: list[dict], budget: int) -> dict:
    """{detector or 'gate': number of runs in which it fires}."""
    dets = sorted({r["detector"] for r in rules})
    count = {d: 0 for d in dets} | {"gate": 0}
    for m in runs:
        s = statistics(reference, m, budget)
        fired = {
            d: any(
                exceeds(r["kind"], s[(d, r["metric"])], r["value"])
                for r in rules
                if r["detector"] == d
            )
            for d in dets
        }
        for d, hit in fired.items():
            count[d] += hit
        count["gate"] += any(fired.values())
    return count


def matrix(reference: dict, faults: dict[str, list[dict]], thresholds: dict[int, list[dict]]):
    rows = []
    for fault, runs in faults.items():
        for b, rules in sorted(thresholds.items()):
            counts = detections(reference, runs, rules, b)
            secs = [x for x in (gate_seconds(m, b) for m in runs) if x is not None]
            for det, k in counts.items():
                lo, hi = clopper_pearson(k, len(runs))
                rows.append(
                    {
                        "fault": fault,
                        "budget": b,
                        "detector": det,
                        "runs": len(runs),
                        "detections": k,
                        "rate": k / len(runs),
                        "ci95_low": lo,
                        "ci95_high": hi,
                        "gate_seconds_mean": sum(secs) / len(secs) if secs else "",
                    }
                )
    return rows


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="analysis.matrix", description=__doc__.splitlines()[0])
    p.add_argument("--reference", type=Path, required=True)
    p.add_argument("--thresholds-dir", type=Path, required=True)
    p.add_argument("--faults", type=Path, nargs="+", required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args(argv)
    reference = json.loads(args.reference.read_text())
    thresholds = {
        b: json.loads((args.thresholds_dir / f"thresholds-{b}.json").read_text())["rules"]
        for b in BUDGETS
    }
    faults = {d.name: load_runs(d) for d in args.faults}
    rows = matrix(reference, faults, thresholds)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
