"""D3: live top-k logprob agreement on greedy continuations (FR-06, PRD Section 6.2.1).

Generation: greedy (temperature 0), 32 new tokens, `n_probs` 10, fixed seed, prompt rendered
through the serving chat template, prompt cache off. An end-of-sequence token counts as a token.

Per prompt:
  agreement share = matched prefix / max(reference length, candidate length), each length capped
                    at 32; the matched prefix is the number of generated tokens identical to the
                    reference before the first difference
  approximate KL  = KL(reference || candidate) at each aligned position up to and including the
                    first difference, over the union of both top-10 sets, probabilities missing
                    from one side floored at 1e-6, both sides renormalised; then averaged over
                    that prompt's aligned positions (never pooled across prompts)
Reported: mean agreement share, and mean and p95 of per-prompt KL across prompts.
"""

from __future__ import annotations

import math

from gate.llama_client import LlamaServer, user_prompt
from gate.stats import mean, percentile

N_NEW = 32
N_PROBS = 10
FLOOR = 1e-6


def measure(server: LlamaServer, prompts: list[dict], seed: int) -> list[dict]:
    out = []
    for p in prompts:
        resp = server.completion(
            user_prompt(server, p["instruction"]),
            n_predict=N_NEW,
            temperature=0,
            n_probs=N_PROBS,
            seed=seed,
            cache_prompt=False,
        )
        steps = resp["completion_probabilities"]
        out.append(
            {
                "id": p["id"],
                "tokens": [s["id"] for s in steps],
                "top": [[[t["id"], t["logprob"]] for t in s["top_logprobs"]] for s in steps],
                "timings": {
                    k: resp["timings"][k]
                    for k in ("prompt_ms", "predicted_ms", "prompt_n", "predicted_n")
                },
            }
        )
    return out


def first_difference(ref: list[int], cand: list[int]) -> int | None:
    for i in range(max(len(ref), len(cand))):
        if i >= len(ref) or i >= len(cand) or ref[i] != cand[i]:
            return i
    return None


def approx_kl(ref_top: list[list], cand_top: list[list], floor: float = FLOOR) -> float:
    p = {tok: math.exp(lp) for tok, lp in ref_top}
    q = {tok: math.exp(lp) for tok, lp in cand_top}
    support = sorted(set(p) | set(q))
    ps = [p.get(t, floor) for t in support]
    qs = [q.get(t, floor) for t in support]
    zp, zq = math.fsum(ps), math.fsum(qs)
    return math.fsum((a / zp) * math.log((a / zp) / (b / zq)) for a, b in zip(ps, qs, strict=True))


def compare_one(ref: dict, cand: dict, n_new: int = N_NEW) -> dict:
    r_tok, c_tok = ref["tokens"][:n_new], cand["tokens"][:n_new]
    diff = first_difference(r_tok, c_tok)
    matched = len(r_tok) if diff is None else diff
    denom = max(len(r_tok), len(c_tok))
    share = 1.0 if denom == 0 else matched / denom
    last = matched if diff is not None else matched - 1  # up to and including the difference
    aligned = min(last + 1, len(ref["top"]), len(cand["top"]))
    kls = [approx_kl(ref["top"][i], cand["top"][i]) for i in range(aligned)]
    return {
        "id": ref["id"],
        "agreement_share": share,
        "first_difference": diff,
        "aligned_positions": aligned,
        "approx_kl": mean(kls) if kls else 0.0,
    }


def metrics(reference: list[dict], candidate: list[dict]) -> dict:
    ref_by_id = {r["id"]: r for r in reference}
    per_prompt = [compare_one(ref_by_id[c["id"]], c) for c in candidate]
    kls = [r["approx_kl"] for r in per_prompt]
    return {
        "n_prompts": len(per_prompt),
        "mean_agreement_share": mean([r["agreement_share"] for r in per_prompt]),
        "mean_approx_kl": mean(kls),
        "p95_approx_kl": percentile(kls, 95),
        "per_prompt": per_prompt,
    }
