"""Gate score (FR-20, PRD Section 6.8) and the thresholds file schema `driftgate.thresholds/2`.

Thresholds file (written only by A/A calibration, T-15; never by hand):

    {"schema": "driftgate.thresholds/2",
     "rules": [{"detector": "D3", "metric": "mean_agreement_share", "kind": "agreement",
                "value": <threshold>}, ...]}

Each rule names a detector, a metric inside that detector's verdict block (dotted path), and how
it is scored, so that a score above 1 means fail:

    kind "upper"      the statistic must stay at or below the threshold
                      score = statistic / threshold                     (threshold >= 0)
    kind "agreement"  an agreement-type metric in [0, 1] that must stay at or above the threshold
                      score = (1 - value) / (1 - threshold)             (threshold <= 1)

A zero margin is allowed (PRD Section 6.8: when every A/A statistic is zero, the threshold is
zero and any change fails): upper threshold 0, or agreement threshold 1. The score is then 0 at
the threshold and `ZERO_MARGIN_SCORE` (a finite stand-in for infinity, so it can be published)
beyond it.

D6 rules must use a ratio to the reference p95 (`ratio.p95_ttft_ms`, `ratio.p95_itl_ms`) with
kind "upper": D6 thresholds are ratios, not milliseconds. D5's paired drop is available as
`accuracy_drop` (reference minus candidate accuracy) for an "upper" rule.

Detector score = the maximum of its rules' scores. Gate score = the maximum over detectors that
have rules. Score only detectors supplied by the current stage: D2 on the build host,
D3/D5/D6 in ValidateService. No applicable rules is an input error. The verdict fails when
the gate score is above 1.
"""

from __future__ import annotations

import math

THRESHOLDS_SCHEMA = "driftgate.thresholds/2"
ZERO_MARGIN_SCORE = 1e6
KINDS = ("upper", "agreement")
D6_RATIO_METRICS = ("ratio.p95_ttft_ms", "ratio.p95_itl_ms")


def _lookup(obj: dict, dotted: str):
    for key in dotted.split("."):
        obj = obj[key]
    return obj


def validate(thresholds: dict) -> list[dict]:
    """Check the schema and every rule; return the rules. Raises ValueError on a bad file."""
    if thresholds.get("schema") != THRESHOLDS_SCHEMA:
        raise ValueError(f"unsupported thresholds schema {thresholds.get('schema')!r}")
    rules = thresholds.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ValueError("thresholds file has no rules")
    for r in rules:
        missing = {"detector", "metric", "kind", "value"} - set(r)
        if missing:
            raise ValueError(f"rule {r} is missing {sorted(missing)}")
        if r["kind"] not in KINDS:
            raise ValueError(f"rule {r}: kind must be one of {KINDS}")
        v = r["value"]
        if not isinstance(v, int | float) or not math.isfinite(v):
            raise ValueError(f"rule {r}: value must be a finite number")
        if r["kind"] == "upper" and v < 0:
            raise ValueError(f"rule {r}: an upper threshold must be >= 0")
        if r["kind"] == "agreement" and not 0 <= v <= 1:
            raise ValueError(f"rule {r}: an agreement threshold must be in [0, 1]")
        if r["detector"] == "D6" and (r["metric"] not in D6_RATIO_METRICS or r["kind"] != "upper"):
            raise ValueError(
                f"rule {r}: D6 thresholds are ratios to the reference p95, "
                f"metric one of {D6_RATIO_METRICS} with kind 'upper'"
            )
    return rules


def rule_score(observed: float, kind: str, threshold: float) -> float:
    numerator, denominator = (
        (observed, threshold) if kind == "upper" else (1 - observed, 1 - threshold)
    )
    if denominator == 0:
        return 0.0 if numerator <= 0 else ZERO_MARGIN_SCORE
    return numerator / denominator


def score(dets: dict, thresholds: dict) -> dict:
    """Per-rule results, per-detector scores and the gate score."""
    results = []
    for r in validate(thresholds):
        if r["detector"] not in dets:
            continue
        if dets[r["detector"]].get("status") == "not run":
            continue
        if dets[r["detector"]].get("status") == "error":
            results.append(
                r
                | {
                    "observed": None,
                    "score": ZERO_MARGIN_SCORE,
                    "ok": False,
                    "error": dets[r["detector"]]["error"],
                }
            )
            continue
        observed = _lookup(dets[r["detector"]], r["metric"])
        s = rule_score(observed, r["kind"], r["value"])
        results.append(r | {"observed": observed, "score": s, "ok": s <= 1})
    if not results:
        raise ValueError("thresholds file has no rules for the measured detectors")
    per_detector: dict[str, float] = {
        d: ZERO_MARGIN_SCORE for d, result in dets.items() if result.get("status") == "error"
    }
    for r in results:
        per_detector[r["detector"]] = max(per_detector.get(r["detector"], -math.inf), r["score"])
    gate = max(per_detector.values())
    return {"rules": results, "scores": per_detector, "gate_score": gate, "passed": gate <= 1}
