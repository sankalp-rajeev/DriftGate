"""D5: exact-match panel of "sometimes right" MMLU items (FR-08, PRD Section 6.2.1).

Each item is asked zero-shot through the serving chat template and answered greedily with one
token; the item is correct when that first generated token, stripped of whitespace, is the gold
letter. Statistic: paired per-item delta in accuracy (candidate minus reference) on identical
items, with a 95% paired-bootstrap percentile interval (10,000 resamples, seed 0) and the exact
McNemar p-value. No threshold here: the gate threshold comes from A/A calibration (FR-12).
"""

from __future__ import annotations

from gate.llama_client import LlamaServer, RequestError, detector_error, user_prompt
from gate.prompts import mmlu_prompt
from gate.stats import mcnemar_exact, mean, paired_bootstrap_ci

RESAMPLES = 10_000
BOOTSTRAP_SEED = 0


def measure(server: LlamaServer, items: list[dict], seed: int) -> list[dict]:
    out = []
    for item in items:
        try:
            resp = server.completion(
                user_prompt(server, mmlu_prompt(item)),
                n_predict=1,
                temperature=0,
                seed=seed,
                cache_prompt=False,
            )
        except RequestError as e:
            out.append({"id": item["id"], "error": e.details})
            break
        predicted = resp["content"].strip()
        out.append(
            {"id": item["id"], "predicted": predicted, "correct": predicted == item["answer"]}
        )
    return out


def metrics(reference: list[dict], candidate: list[dict]) -> dict:
    if error := detector_error(candidate):
        return error
    ref_by_id = {r["id"]: r for r in reference}
    ids = [c["id"] for c in candidate]
    ref = [int(ref_by_id[i]["correct"]) for i in ids]
    cand = [int(c["correct"]) for c in candidate]
    lo, hi = paired_bootstrap_ci(ref, cand, RESAMPLES, BOOTSTRAP_SEED)
    return {
        "n_items": len(ids),
        "reference_accuracy": mean(ref),
        "candidate_accuracy": mean(cand),
        "delta_accuracy": mean(cand) - mean(ref),
        "accuracy_drop": mean(ref) - mean(cand),  # for an "upper" threshold rule (gate score)
        "ci95": [lo, hi],
        "bootstrap": {"resamples": RESAMPLES, "seed": BOOTSTRAP_SEED, "method": "percentile"},
        "mcnemar": mcnemar_exact(ref, cand),
    }
