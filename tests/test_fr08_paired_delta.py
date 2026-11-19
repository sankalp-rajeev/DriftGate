"""FR-08 (D5): paired per-item accuracy delta with bootstrap interval and exact McNemar."""

import pytest

from gate import d5, stats


def _runs(ref_correct, cand_correct):
    ref = [{"id": i, "correct": bool(c)} for i, c in enumerate(ref_correct)]
    cand = [{"id": i, "correct": bool(c)} for i, c in enumerate(cand_correct)]
    return ref, cand


def test_mcnemar_exact_known_values():
    # 5 discordant pairs, all in one direction: p = 2 * (1/2)^5
    r = stats.mcnemar_exact([1] * 5 + [0] * 5, [0] * 5 + [0] * 5)
    assert (r["b_reference_only"], r["c_candidate_only"]) == (5, 0)
    assert r["p_value"] == pytest.approx(2 / 32)
    assert stats.mcnemar_exact([1, 0], [1, 0])["p_value"] == 1.0


def test_bootstrap_is_deterministic_and_brackets_the_delta():
    ref = [1, 0, 1, 1, 0, 1, 0, 1, 1, 0] * 3
    cand = [1, 0, 0, 1, 0, 0, 0, 1, 1, 0] * 3
    a = stats.paired_bootstrap_ci(ref, cand, 2000, seed=0)
    b = stats.paired_bootstrap_ci(ref, cand, 2000, seed=0)
    assert a == b
    delta = (sum(cand) - sum(ref)) / len(ref)
    assert a[0] <= delta <= a[1]
    assert a[1] < 0  # the candidate is worse on every discordant pair


def test_identical_runs_give_zero_delta_and_degenerate_interval():
    ref, cand = _runs([1, 0, 1, 1], [1, 0, 1, 1])
    m = d5.metrics(ref, cand)
    assert m["delta_accuracy"] == 0.0
    assert m["ci95"] == [0.0, 0.0]
    assert m["mcnemar"]["p_value"] == 1.0
    assert m["bootstrap"] == {"resamples": 10_000, "seed": 0, "method": "percentile"}


def test_delta_is_candidate_minus_reference_on_identical_items():
    ref, cand = _runs([1, 1, 1, 0], [0, 1, 1, 1])
    cand = list(reversed(cand))  # order must not matter: pairing is by id
    m = d5.metrics(ref, cand)
    assert m["reference_accuracy"] == 0.75
    assert m["candidate_accuracy"] == 0.75
    assert m["delta_accuracy"] == 0.0
    assert m["mcnemar"]["b_reference_only"] == 1
    assert m["mcnemar"]["c_candidate_only"] == 1
