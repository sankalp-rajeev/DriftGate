"""FR-09 (D6): p95 time to first token and inter-token latency from server timings."""

import pytest

from gate import d6, stats


def _run(prompt_ms, predicted_ms, predicted_n=32):
    return {
        "id": 0,
        "timings": {
            "prompt_ms": prompt_ms,
            "predicted_ms": predicted_ms,
            "prompt_n": 10,
            "predicted_n": predicted_n,
        },
    }


def test_percentile_linear_interpolation():
    assert stats.percentile([1, 2, 3, 4, 5], 50) == 3
    assert stats.percentile(list(range(1, 101)), 95) == pytest.approx(95.05)
    assert stats.percentile([7.0], 95) == 7.0


def test_p95_ttft_and_itl_vs_baseline():
    ref = [_run(100 + i, 320) for i in range(20)]
    cand = [_run(200 + i, 640) for i in range(20)]
    m = d6.metrics(ref, cand)
    assert m["reference"]["p95_ttft_ms"] == pytest.approx(stats.percentile(range(100, 120), 95))
    assert m["reference"]["p95_itl_ms"] == pytest.approx(10.0)
    assert m["candidate"]["p95_itl_ms"] == pytest.approx(20.0)
    assert m["delta_ms"]["p95_itl_ms"] == pytest.approx(10.0)
    assert m["ratio"]["p95_itl_ms"] == pytest.approx(2.0)


def test_requests_with_no_generated_tokens_are_left_out_of_itl():
    m = d6.p95s([_run(10, 0, predicted_n=0), _run(10, 50, predicted_n=5)])
    assert m["p95_itl_ms"] == 10.0
