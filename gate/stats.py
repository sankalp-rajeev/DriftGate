"""Small, dependency-free statistics used by the detectors (deterministic given a seed)."""

from __future__ import annotations

import math
import random
from collections.abc import Sequence


def percentile(values: Sequence[float], q: float) -> float:
    """q-th percentile (0..100) with linear interpolation between order statistics."""
    if not values:
        raise ValueError("percentile of empty sequence")
    xs = sorted(values)
    pos = (len(xs) - 1) * q / 100
    lo = math.floor(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def mean(values: Sequence[float]) -> float:
    return math.fsum(values) / len(values)


def paired_bootstrap_ci(
    ref: Sequence[int], cand: Sequence[int], resamples: int, seed: int, level: float = 0.95
) -> tuple[float, float]:
    """Percentile interval for mean(cand - ref) by resampling items with replacement."""
    if len(ref) != len(cand) or not ref:
        raise ValueError("paired samples must be non-empty and equal length")
    diffs = [c - r for r, c in zip(ref, cand, strict=True)]
    n = len(diffs)
    rng = random.Random(seed)
    stats = []
    for _ in range(resamples):
        stats.append(sum(diffs[rng.randrange(n)] for _ in range(n)) / n)
    alpha = (1 - level) / 2 * 100
    return percentile(stats, alpha), percentile(stats, 100 - alpha)


def mcnemar_exact(ref: Sequence[int], cand: Sequence[int]) -> dict:
    """Exact (binomial) two-sided McNemar test on paired binary outcomes."""
    b = sum(1 for r, c in zip(ref, cand, strict=True) if r and not c)  # reference-only correct
    c = sum(1 for r, k in zip(ref, cand, strict=True) if k and not r)  # candidate-only correct
    n = b + c
    if n == 0:
        p = 1.0
    else:
        k = min(b, c)
        tail = math.fsum(math.comb(n, i) for i in range(k + 1)) / 2**n
        p = min(1.0, 2 * tail)
    return {"b_reference_only": b, "c_candidate_only": c, "p_value": p}
