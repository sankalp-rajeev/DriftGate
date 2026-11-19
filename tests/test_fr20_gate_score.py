"""FR-20 (PRD Section 6.8): per-detector score, oriented so above 1 means fail; gate score = max.

Rule values here are placeholders that exercise the arithmetic, not calibrated thresholds.
"""

import pytest

from gate import score

DETS = {
    "D3": {"mean_agreement_share": 0.8, "p95_approx_kl": 0.05},
    "D5": {"accuracy_drop": 0.02},
    "D6": {"ratio": {"p95_ttft_ms": 1.3, "p95_itl_ms": 0.9}},
}


def _t(*rules):
    return {"schema": score.THRESHOLDS_SCHEMA, "rules": list(rules)}


def _r(detector, metric, kind, value):
    return {"detector": detector, "metric": metric, "kind": kind, "value": value}


def test_upper_score_is_statistic_over_threshold():
    s = score.score(DETS, _t(_r("D3", "p95_approx_kl", "upper", 0.1)))
    assert s["scores"]["D3"] == pytest.approx(0.5)
    assert s["passed"] is True


def test_agreement_score_is_one_minus_value_over_one_minus_threshold():
    s = score.score(DETS, _t(_r("D3", "mean_agreement_share", "agreement", 0.9)))
    assert s["scores"]["D3"] == pytest.approx((1 - 0.8) / (1 - 0.9))
    assert s["gate_score"] > 1
    assert s["passed"] is False
    assert s["rules"][0]["ok"] is False


def test_detector_score_is_max_of_its_rules_and_gate_score_is_max_over_detectors():
    s = score.score(
        DETS,
        _t(
            _r("D3", "mean_agreement_share", "agreement", 0.5),  # 0.4
            _r("D3", "p95_approx_kl", "upper", 0.0625),  # 0.8
            _r("D5", "accuracy_drop", "upper", 0.04),  # 0.5
            _r("D6", "ratio.p95_ttft_ms", "upper", 1.5),  # 0.8667
        ),
    )
    assert s["scores"] == {
        "D3": pytest.approx(0.8),
        "D5": pytest.approx(0.5),
        "D6": pytest.approx(1.3 / 1.5),
    }
    assert s["gate_score"] == pytest.approx(1.3 / 1.5)
    assert s["passed"] is True


def test_score_of_exactly_one_passes():
    s = score.score(DETS, _t(_r("D5", "accuracy_drop", "upper", 0.02)))
    assert s["gate_score"] == pytest.approx(1.0)
    assert s["passed"] is True


@pytest.mark.parametrize(
    "rule",
    [
        _r("D6", "candidate.p95_ttft_ms", "upper", 500.0),  # milliseconds are not allowed
        _r("D6", "ratio.p95_ttft_ms", "agreement", 0.5),
        _r("D3", "p95_approx_kl", "upper", -0.1),
        _r("D3", "mean_agreement_share", "agreement", 1.1),
        _r("D3", "mean_agreement_share", "lower", 0.5),
    ],
)
def test_invalid_rules_are_rejected(rule):
    with pytest.raises(ValueError):
        score.score(DETS, _t(rule))


def test_old_schema_is_rejected():
    with pytest.raises(ValueError, match="schema"):
        score.validate({"schema": "driftgate.thresholds/1", "rules": []})
