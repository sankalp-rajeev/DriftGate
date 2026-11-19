"""FR-12 and FR-13: A/A calibration, held-out false-alarm estimate and the fault matrix.

Pure computation on synthetic measurements: no model and no server are run.
"""

import csv
import json
import math

import pytest

from analysis import calibrate, matrix, rq2
from gate import d5, score
from gate.cli import MEASUREMENT_SCHEMA

N = 256


@pytest.fixture(autouse=True)
def _fast_bootstrap(monkeypatch):
    monkeypatch.setattr(d5, "RESAMPLES", 50)  # the interval is not used by calibration


def measurement(jitter=0.0, flip=0, diverge=False, seed=0):
    d3 = []
    for i in range(N):
        tokens = [1, 2, 3, 4] if not (diverge and i % 2 == 0) else [1, 9, 3, 4]
        top = [[[t, -0.1], [t + 100, -2.5]] for t in tokens]
        d3.append(
            {
                "id": i,
                "tokens": tokens,
                "top": top,
                "timings": {
                    "prompt_ms": 100.0 + jitter * (i % 7),
                    "predicted_ms": 400.0 + jitter * (i % 5),
                    "prompt_n": 20,
                    "predicted_n": 4,
                },
            }
        )
    d5 = [{"id": i, "predicted": "A", "correct": i % 2 == 0 and i >= flip} for i in range(N)]
    inputs = {"d3_prompts_sha256": "a", "d5_panel_sha256": "b"}
    return {
        "schema": MEASUREMENT_SCHEMA,
        "budget": N,
        "seed": seed,
        "server": {},
        "inputs": inputs,
        "wall_clock_s": 1.0,
        "d3": d3,
        "d5": d5,
    }


REFERENCE = measurement()


def aa_runs(n=30, scale=1.0):
    return [measurement(jitter=scale * (1 + k % 4)) for k in range(n)]


def test_budgets_are_prefixes_of_one_run():
    m = measurement()
    cut = calibrate.at_budget(m, 16)
    assert len(cut["d3"]) == 16 and len(cut["d5"]) == 16 and cut["budget"] == 16
    assert len(m["d3"]) == N  # the input is not modified


def test_threshold_is_the_worst_aa_value_with_no_margin():
    assert calibrate.threshold_value("upper", [0.1, 0.4, 0.2]) == 0.4
    assert calibrate.threshold_value("agreement", [0.9, 0.8, 1.0]) == pytest.approx(0.8)
    assert calibrate.threshold_value("upper", [-0.2, -0.1]) == 0.0  # never negative
    assert calibrate.threshold_value("upper", [0.0, 0.0]) == 0.0  # all zero: RQ3 result
    assert calibrate.threshold_value("agreement", [1.0, 1.0]) == 1.0


def test_fail_means_strictly_above_the_threshold():
    assert not calibrate.exceeds("upper", 0.4, 0.4)
    assert calibrate.exceeds("upper", 0.41, 0.4)
    assert not calibrate.exceeds("agreement", 0.8, 0.8)
    assert calibrate.exceeds("agreement", 0.79, 0.8)


def test_calibration_uses_20_runs_and_holds_out_10():
    res = calibrate.calibrate(REFERENCE, aa_runs())
    assert res["n_calibration"] == 20 and res["n_held_out"] == 10
    assert set(res["budgets"]) == {"16", "64", "256"}
    assert res["budgets"]["256"]["per_statistic_bound"] == pytest.approx(1 / 21)
    with pytest.raises(ValueError):
        calibrate.calibrate(REFERENCE, aa_runs(29))


def test_thresholds_file_is_valid_and_held_out_rate_has_an_interval():
    res = calibrate.calibrate(REFERENCE, aa_runs())
    info = res["budgets"]["256"]
    score.validate({"schema": score.THRESHOLDS_SCHEMA, "rules": info["rules"]})
    d6 = next(r for r in info["rules"] if r["metric"] == "ratio.p95_ttft_ms")
    assert d6["kind"] == "upper" and d6["value"] > 1  # a ratio to the reference p95
    fa = info["held_out_false_alarms"]["gate"]
    assert fa["n"] == 10 and 0 <= fa["ci95"][0] <= fa["rate"] <= fa["ci95"][1] <= 1


def test_all_zero_aa_gives_zero_thresholds_and_any_change_fails():
    runs = [measurement() for _ in range(30)]
    res = calibrate.calibrate(REFERENCE, runs, budgets=(16,))
    rules = {(r["detector"], r["metric"]): r["value"] for r in res["budgets"]["16"]["rules"]}
    assert rules[("D3", "mean_approx_kl")] == 0.0
    assert rules[("D3", "mean_agreement_share")] == 1.0
    assert rules[("D5", "accuracy_drop")] == 0.0
    changed = calibrate.statistics(REFERENCE, measurement(diverge=True), 16)
    assert calibrate.exceeds("upper", changed[("D3", "mean_approx_kl")], 0.0)
    # the gate score handles a zero margin: finite, above 1 only when the statistic moves
    dets = {"D3": {"mean_approx_kl": 0.0}}
    th = {
        "schema": score.THRESHOLDS_SCHEMA,
        "rules": [{"detector": "D3", "metric": "mean_approx_kl", "kind": "upper", "value": 0.0}],
    }
    assert score.score(dets, th)["gate_score"] == 0.0
    dets["D3"]["mean_approx_kl"] = 0.01
    s = score.score(dets, th)
    assert s["gate_score"] > 1 and math.isfinite(s["gate_score"]) and not s["passed"]


def test_clopper_pearson_known_values():
    lo, hi = calibrate.clopper_pearson(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.3085, abs=1e-3)
    lo, hi = calibrate.clopper_pearson(1, 10)
    assert (lo, hi) == pytest.approx((0.0025, 0.4450), abs=1e-3)
    assert calibrate.clopper_pearson(10, 10)[1] == 1.0


def test_budget_selection_takes_the_smallest_that_meets_the_target():
    res = {
        "budgets": {
            "16": {
                "held_out_false_alarms": {"D3": {"rate": 0.1}, "gate": {"rate": 0.1}},
                "gate_seconds_mean": 10,
            },
            "64": {
                "held_out_false_alarms": {"D3": {"rate": 0.0}, "gate": {"rate": 0.0}},
                "gate_seconds_mean": 700,
            },
            "256": {
                "held_out_false_alarms": {"D3": {"rate": 0.0}, "gate": {"rate": 0.0}},
                "gate_seconds_mean": 500,
            },
        }
    }
    assert calibrate.select_budget(res, 0.05, 600) == 256  # 16 fails FPR, 64 fails wall-clock
    assert calibrate.select_budget(res, 0.05, 100) is None


def test_cli_writes_thresholds_per_budget_and_installs_the_selected_one(tmp_path):
    runs, ref = tmp_path / "aa", tmp_path / "ref.json"
    runs.mkdir()
    ref.write_text(json.dumps(REFERENCE))
    for i, m in enumerate(aa_runs()):
        (runs / f"run-{i:02d}.measurement.json").write_text(json.dumps(m))
    out, installed = tmp_path / "out", tmp_path / "thresholds.json"
    args = ["--runs", str(runs), "--reference", str(ref), "--out-dir", str(out)]
    assert calibrate.main(args + ["--target-fpr", "1", "--write-thresholds", str(installed)]) == 0
    for b in (16, 64, 256):
        assert (out / f"thresholds-{b}.json").is_file()
    assert (out / "calibration.json").is_file()
    assert json.loads(installed.read_text())["schema"] == score.THRESHOLDS_SCHEMA


def test_matrix_detects_a_fault_and_not_aa(tmp_path):
    res = calibrate.calibrate(REFERENCE, aa_runs())
    thresholds = {int(b): v["rules"] for b, v in res["budgets"].items()}
    faults = {
        "AA": aa_runs(10),
        "F4": [measurement(jitter=1.0, diverge=True, flip=40) for _ in range(10)],
    }
    rows = matrix.matrix(REFERENCE, faults, thresholds)
    cell = {(r["fault"], r["budget"], r["detector"]): r for r in rows}
    assert cell[("F4", 256, "D3")]["rate"] == 1.0
    assert cell[("F4", 256, "gate")]["detections"] == 10
    assert cell[("AA", 256, "gate")]["rate"] < 1.0
    assert cell[("F4", 16, "D3")]["ci95_low"] > 0.5
    assert cell[("F4", 64, "D3")]["gate_seconds_mean"] > 0
    assert {r["budget"] for r in rows} == {16, 64, 256}
    d = tmp_path / "F4"
    d.mkdir()
    for i, m in enumerate(faults["F4"]):
        (d / f"run-{i:02d}.measurement.json").write_text(json.dumps(m))
    ref = tmp_path / "ref.json"
    ref.write_text(json.dumps(REFERENCE))
    th = tmp_path / "th"
    th.mkdir()
    for b, rules in thresholds.items():
        (th / f"thresholds-{b}.json").write_text(
            json.dumps({"schema": score.THRESHOLDS_SCHEMA, "rules": rules})
        )
    out = tmp_path / "matrix.csv"
    args = ["--reference", str(ref), "--thresholds-dir", str(th), "--faults", str(d)]
    assert matrix.main(args + ["--out", str(out)]) == 0
    assert next(csv.reader(out.open())) == matrix.COLUMNS


def test_rq2_spearman_with_exact_permutation_p():
    pts = [
        {"name": str(i), "mean_kld": k, "accuracy_drop": d}
        for i, (k, d) in enumerate(
            [(0.0, 0.0), (0.01, 0.01), (0.05, 0.03), (0.2, 0.1), (0.5, 0.25), (1.2, 0.5)]
        )
    ]
    r = rq2.analyse(pts)
    assert r["spearman_rho"] == pytest.approx(1.0)
    assert r["permutation_p_two_sided"] == pytest.approx(2 / 720)
    assert rq2.spearman([1, 2, 3], [3, 2, 1]) == pytest.approx(-1.0)
    assert rq2.ranks([5, 5, 1]) == [2.5, 2.5, 1.0]
    with pytest.raises(ValueError):
        rq2.analyse(pts[:2])
