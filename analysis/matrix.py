"""Fault matrix (FR-13): detection rate, interval and wall-clock per fault, detector and level.

Input: `--faults` directories, one per fault (name = directory name, e.g. runs/.../F4), each
holding `run-NN.measurement.json` files (and `run-NN.d2.json`, `run-NN.d1.json` where D2 and D1
ran) from `python -m faults.aa --level L3 --bundle <fault bundle>`; one run at L3 covers L1 and
L2. `--thresholds-dir` is the output of `analysis.calibrate`. A run is a detection for a
detector when its gating statistic exceeds the threshold (D1: the check fails); the `gate` row
counts runs in which any detector fires. A detector with no results for a fault (D2 runs only
for A/A, F1, F2, F3, F8 and the sweep) has no row for it. The interval is Clopper-Pearson 95%
over the runs completed. Wall-clock is reported in seconds for the first D3 prompts of the
level, from llama-server timings on the machine that ran them (informational: NFR-01 is
checked in the Lab).

  python -m analysis.matrix --reference <measurement.json> --thresholds-dir <dir> \\
      --faults runs/2026-11-24-study/F1 runs/2026-11-24-study/F4 --out runs/.../matrix.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from analysis.calibrate import LEVELS, clopper_pearson, fired, load_runs, statistics

COLUMNS = ["fault", "level", "detector", "runs", "detections", "rate", "ci95_low", "ci95_high"]
COLUMNS += ["d3_seconds_mean"]


def d3_seconds(m: dict, level: str) -> float | None:
    try:
        return sum(
            (r["timings"]["prompt_ms"] + r["timings"]["predicted_ms"]) / 1000
            for r in m["d3"][: LEVELS[level][0]]
        )
    except KeyError:
        return None


def detections(reference: dict, runs: list[dict], rules: list[dict], level: str) -> dict:
    """{detector or 'gate': (runs evaluated, runs in which it fires)}."""
    evaluated: dict[str, int] = {}
    hits: dict[str, int] = {}
    gate_runs = gate_hits = 0
    for m in runs:
        results = fired(statistics(reference, m, level), rules, m.get("d1_ok"))
        any_hit = False
        for det, hit in results.items():
            if hit is None:
                continue
            evaluated[det] = evaluated.get(det, 0) + 1
            hits[det] = hits.get(det, 0) + bool(hit)
            any_hit |= bool(hit)
        if any(h is not None for h in results.values()):
            gate_runs += 1
            gate_hits += any_hit
    out = {d: (evaluated[d], hits[d]) for d in sorted(evaluated)}
    out["gate"] = (gate_runs, gate_hits)
    return out


def matrix(reference: dict, faults: dict[str, list[dict]], thresholds: dict[str, list[dict]]):
    rows = []
    for fault, runs in faults.items():
        for level, rules in sorted(thresholds.items(), key=lambda kv: LEVELS[kv[0]]):
            secs = [x for x in (d3_seconds(m, level) for m in runs) if x is not None]
            for det, (n, k) in detections(reference, runs, rules, level).items():
                lo, hi = clopper_pearson(k, n)
                rows.append(
                    {
                        "fault": fault,
                        "level": level,
                        "detector": det,
                        "runs": n,
                        "detections": k,
                        "rate": k / n,
                        "ci95_low": lo,
                        "ci95_high": hi,
                        "d3_seconds_mean": sum(secs) / len(secs) if secs else "",
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
        lv: json.loads((args.thresholds_dir / f"thresholds-{lv}.json").read_text())["rules"]
        for lv in LEVELS
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
