"""FR-06 (D3): top-k logprob agreement and approximate KL on greedy continuations."""

import math

import pytest

from gate import d3

LN = math.log


def _step(*pairs):
    return [[tok, LN(p)] for tok, p in pairs]


def test_identical_distributions_have_zero_kl():
    top = _step((1, 0.7), (2, 0.2), (3, 0.1))
    assert d3.approx_kl(top, top) == pytest.approx(0.0, abs=1e-12)


def test_kl_uses_union_with_floor_and_renormalisation():
    ref = _step((1, 0.6), (2, 0.4))
    cand = _step((1, 0.5), (3, 0.5))
    floor = 1e-6
    p = [0.6, 0.4, floor]
    q = [0.5, floor, 0.5]
    zp, zq = sum(p), sum(q)
    expected = sum((a / zp) * math.log((a / zp) / (b / zq)) for a, b in zip(p, q, strict=True))
    assert d3.approx_kl(ref, cand) == pytest.approx(expected)
    assert d3.approx_kl(ref, cand) > 0


def _run(pid, tokens):
    return {"id": pid, "tokens": tokens, "top": [_step((t, 0.9), (t + 1, 0.1)) for t in tokens]}


def test_identical_sequences_score_full_agreement_and_kl_over_all_positions():
    r = d3.compare_one(_run(0, [5, 6, 7]), _run(0, [5, 6, 7]))
    assert r["agreement_share"] == 1.0
    assert r["first_difference"] is None
    assert r["aligned_positions"] == 3
    assert r["approx_kl"] == pytest.approx(0.0, abs=1e-12)


def test_agreement_share_is_matched_prefix_over_longer_length():
    ref = _run(0, list(range(100, 132)))
    cand = _run(0, list(range(100, 108)) + list(range(200, 224)))
    r = d3.compare_one(ref, cand)
    assert r["first_difference"] == 8
    assert r["agreement_share"] == 8 / 32
    assert r["aligned_positions"] == 9  # positions 0..8, including the first difference


def test_eos_counts_as_a_token_and_lengths_cap_at_32():
    # Both stop early at the same EOS (id 0): identical, share 1.
    assert d3.compare_one(_run(0, [1, 2, 0]), _run(0, [1, 2, 0]))["agreement_share"] == 1.0
    # Candidate stops early: matched 2 of max(4, 3) tokens.
    r = d3.compare_one(_run(0, [1, 2, 3, 4]), _run(0, [1, 2, 0]))
    assert r["first_difference"] == 2
    assert r["agreement_share"] == 2 / 4
    # Over-long sequences are capped at 32 before comparing.
    long_ref, long_cand = _run(0, [1] * 40), _run(0, [1] * 32 + [2] * 8)
    assert d3.compare_one(long_ref, long_cand)["agreement_share"] == 1.0


def test_kl_is_averaged_per_prompt_then_summarised_across_prompts():
    def step(p):
        return [[1, math.log(p)], [2, math.log(1 - p)]]

    ref = [{"id": i, "tokens": [1, 1], "top": [step(0.9), step(0.9)]} for i in range(3)]
    cand = [
        {"id": 0, "tokens": [1, 1], "top": [step(0.9), step(0.9)]},
        {"id": 1, "tokens": [1, 1], "top": [step(0.5), step(0.9)]},
        {"id": 2, "tokens": [1, 1], "top": [step(0.5), step(0.5)]},
    ]
    k = d3.approx_kl(step(0.9), step(0.5))
    m = d3.metrics(ref, cand)
    per = [r["approx_kl"] for r in m["per_prompt"]]
    assert per == pytest.approx([0.0, k / 2, k])
    assert m["mean_approx_kl"] == pytest.approx(k / 2)
    assert m["p95_approx_kl"] == pytest.approx(k / 2 + 0.9 * (k - k / 2))
    assert m["mean_agreement_share"] == 1.0
    assert m["n_prompts"] == 3
