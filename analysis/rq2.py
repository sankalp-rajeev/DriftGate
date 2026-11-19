"""RQ2: does D2 mean KL track the D5 accuracy drop? (pre-registered, PRD Section 6.8)

Spearman rank correlation between D2 mean KL and the D5 accuracy drop over the quantization
sweep (Q8_0, Q6_K, Q5_K_M, Q4_K_M, Q3_K_M, Q2_K) plus F3, with an exact two-sided permutation
p-value (all n! pairings; ties get average ranks).

Input: a JSON list of points `{"name": ..., "mean_kld": ..., "accuracy_drop": ...}`.

  python -m analysis.rq2 --points runs/2026-11-25-rq2/points.json --out runs/.../rq2.json
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path


def ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return out


def pearson(x: list[float], y: list[float]) -> float:
    n = len(x)
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx == 0 or syy == 0:
        return 0.0
    return sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True)) / (sxx * syy) ** 0.5


def spearman(x: list[float], y: list[float]) -> float:
    return pearson(ranks(x), ranks(y))


def permutation_p(x: list[float], y: list[float]) -> float:
    """Exact two-sided p: share of all pairings with |rho| at least the observed |rho|."""
    observed = abs(spearman(x, y))
    rx = ranks(x)
    total = hits = 0
    for perm in itertools.permutations(ranks(y)):
        total += 1
        hits += abs(pearson(rx, list(perm))) >= observed - 1e-12
    return hits / total


def analyse(points: list[dict]) -> dict:
    if len(points) < 3:
        raise ValueError("need at least 3 points")
    x = [p["mean_kld"] for p in points]
    y = [p["accuracy_drop"] for p in points]
    return {
        "n": len(points),
        "spearman_rho": spearman(x, y),
        "permutation_p_two_sided": permutation_p(x, y),
        "points": points,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="analysis.rq2", description=__doc__.splitlines()[0])
    p.add_argument("--points", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args(argv)
    result = analyse(json.loads(args.points.read_text()))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
