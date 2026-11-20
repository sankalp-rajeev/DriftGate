"""D6: latency from llama-server response timings over the D3 run (FR-09).

Per request: time to first token = `timings.prompt_ms` (the first token is sampled from the
prompt pass); inter-token latency = `timings.predicted_ms / timings.predicted_n`. Reported: p95 of
each over the run, for candidate and baseline, with the candidate-minus-baseline difference and
ratio. The budget threshold comes from A/A calibration, not from here.
"""

from __future__ import annotations

from gate.llama_client import detector_error
from gate.stats import percentile


def p95s(d3_run: list[dict]) -> dict:
    ttft = [r["timings"]["prompt_ms"] for r in d3_run]
    itl = [
        r["timings"]["predicted_ms"] / r["timings"]["predicted_n"]
        for r in d3_run
        if r["timings"]["predicted_n"] > 0
    ]
    return {"p95_ttft_ms": percentile(ttft, 95), "p95_itl_ms": percentile(itl, 95)}


def metrics(reference: list[dict], candidate: list[dict]) -> dict:
    if error := detector_error(candidate):
        return error
    ref, cand = p95s(reference), p95s(candidate)
    return {
        "n_requests": len(candidate),
        "reference": ref,
        "candidate": cand,
        "delta_ms": {k: cand[k] - ref[k] for k in ref},
        "ratio": {k: cand[k] / ref[k] if ref[k] else None for k in ref},
    }
