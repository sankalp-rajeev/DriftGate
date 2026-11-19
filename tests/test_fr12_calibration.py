"""FR-12 and FR-13: A/A calibration, held-out false-alarm estimate and the fault matrix.

Pure computation on synthetic measurements: no model and no server are run.
"""

import csv
import json
import math
from pathlib import Path

import pytest

from analysis import calibrate, matrix, rq2
from gate import d5, score
from gate.cli import MEASUREMENT_SCHEMA
from gate.manifest import load_yaml

N = 256
L = "L3"


@pytest.fixture(autouse=True)
def _fast_bootstrap(monkeypatch):
    monkeypatch.setattr(d5, "RESAMPLES", 50)  # the interval is not used by calibration


def measurement(jitter=0.0, flip=0, diverge=False, kld=0.0, d2=True):
    d3 = []
    for i in range(N):
        tokens = [1, 9, 3, 4] if diverge and i % 2 == 0 else [1, 2, 3, 4]
        top = [[[t, -0.1], [t + 100, -2.5]] for t in tokens]
        timings = {
            "prompt_ms": 100.0 + jitter * (i % 7),
            "predicted_ms": 400.0 + jitter * (i % 5),
            "prompt_n": 20,
            "predicted_n": 4,
        }
        d3.append({"id": i, "tokens": tokens, "top": top, "timings": timings})
    d5_run = [{"id": i, "predicted": "A", "correct": i % 2 == 0 and i >= flip} for i in range(N)]
    m = {
        "schema": MEASUREMENT_SCHEMA,
        "budget": N,
        "seed": 0,
        "server": {},
        "inputs": {"d3_prompts_sha256": "a", "d5_panel_sha256": "b"},
        "wall_clock_s": 1.0,
        "d3": d3,
        "d5": d5_run,
    }
    if d2:
        m["d2"] = {"mean_kld": kld, "p99_kld": kld * 3, "same_top_p_pct": 100.0 - kld}
    return m


REFERENCE = measurement(d2=False)


def aa_runs(n=30, scale=1.0, kld=0.0):
    return [measurement(jitter=scale * (1 + k % 4), kld=kld * (k % 3)) for k in range(n)]


def test_levels_match_the_gate_config():
    cfg = load_yaml(Path("gate/config.yaml"))["levels"]
    assert {k: (v["d3"], v["d5"]) for k, v in cfg.items()} == calibrate.LEVELS
    assert calibrate.LEVELS == {"L1": (16, 64), "L2": (32, 128), "L3": (64, 256)}


def test_levels_are_prefixes_of_one_run():
    m = measurement()
    cut = calibrate.at_budget(m, "L1")
    assert len(cut["d3"]) == 16 and len(cut["d5"]) == 64
    assert len(calibrate.at_budget(m, "L2")["d3"]) == 32
    assert len(m["d3"]) == N  # the input is not modified


def test_one_gating_statistic_per_detector_and_the_rest_reported():
    assert [(d, m) for d, m, _ in calibrate.STATISTICS] == [
        ("D2", "mean_kld"),
        ("D3", "mean_agreement_share"),
        ("D5", "accuracy_drop"),
        ("D6", "ratio.p95_itl_ms"),
    ]
    gated = {(d, m) for d, m, _ in calibrate.STATISTICS}
    assert gated.isdisjoint(calibrate.REPORTED)
    res = calibrate.calibrate(REFERENCE, aa_runs(), levels=("L3",))
    info = res["levels"]["L3"]
    assert len(info["rules"]) == 4
    assert "D3.mean_approx_kl" in info["reported_aa_range"]  # reported, never gated
    score.validate({"schema": score.THRESHOLDS_SCHEMA, "rules": info["rules"]})


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


def test_calibration_uses_20_runs_holds_out_10_and_needs_d2():
    res = calibrate.calibrate(REFERENCE, aa_runs())
    assert res["n_calibration"] == 20 and res["n_held_out"] == 10
    assert set(res["levels"]) == {"L1", "L2", "L3"}
    assert res["levels"]["L3"]["per_detector_bound"] == pytest.approx(1 / 21)
    assert res["levels"]["L3"]["gate_bound"] is None  # no bound is claimed at gate level
    with pytest.raises(ValueError):
        calibrate.calibrate(REFERENCE, aa_runs(29))
    no_d2 = [measurement(d2=False) for _ in range(30)]
    with pytest.raises(ValueError, match="D2"):
        calibrate.calibrate(REFERENCE, no_d2)


def test_held_out_false_alarms_per_detector_and_gate_with_interval():
    res = calibrate.calibrate(REFERENCE, aa_runs(kld=0.01))
    fa = res["levels"]["L3"]["held_out_false_alarms"]
    assert set(fa) == {"D2", "D3", "D5", "D6", "gate"}
    for v in fa.values():
        assert v["n"] == 10 and 0 <= v["ci95"][0] <= v["rate"] <= v["ci95"][1] <= 1
    d6 = next(r for r in res["levels"]["L3"]["rules"] if r["detector"] == "D6")
    assert d6["metric"] == "ratio.p95_itl_ms" and d6["value"] > 1  # a ratio to the reference p95


def test_all_zero_aa_gives_zero_thresholds_and_any_change_fails():
    runs = [measurement() for _ in range(30)]
    res = calibrate.calibrate(REFERENCE, runs, levels=("L1",))
    rules = {(r["detector"], r["metric"]): r["value"] for r in res["levels"]["L1"]["rules"]}
    assert rules[("D2", "mean_kld")] == 0.0
    assert rules[("D3", "mean_agreement_share")] == 1.0
    assert rules[("D5", "accuracy_drop")] == 0.0
    changed = calibrate.statistics(REFERENCE, measurement(diverge=True, kld=0.2), "L1")
    assert calibrate.exceeds("agreement", changed[("D3", "mean_agreement_share")], 1.0)
    assert calibrate.exceeds("upper", changed[("D2", "mean_kld")], 0.0)
    th = {
        "schema": score.THRESHOLDS_SCHEMA,
        "rules": [{"detector": "D2", "metric": "mean_kld", "kind": "upper", "value": 0.0}],
    }
    assert score.score({"D2": {"mean_kld": 0.0}}, th)["gate_score"] == 0.0
    s = score.score({"D2": {"mean_kld": 0.01}}, th)
    assert s["gate_score"] > 1 and math.isfinite(s["gate_score"]) and not s["passed"]


def test_clopper_pearson_known_values():
    lo, hi = calibrate.clopper_pearson(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.3085, abs=1e-3)
    lo, hi = calibrate.clopper_pearson(1, 10)
    assert (lo, hi) == pytest.approx((0.0025, 0.4450), abs=1e-3)
    assert calibrate.clopper_pearson(10, 10)[1] == 1.0


def _fa(**k):
    return {d: {"k": v} for d, v in k.items()} | {"gate": {"k": 9}}


def test_level_is_eligible_unless_a_detector_has_two_or_more_false_alarms():
    res = {
        "levels": {
            "L1": {"held_out_false_alarms": _fa(D2=0, D3=2, D5=0, D6=0)},
            "L2": {"held_out_false_alarms": _fa(D2=1, D3=1, D5=1, D6=1)},
            "L3": {"held_out_false_alarms": _fa(D2=0, D3=0, D5=0, D6=0)},
        }
    }
    assert calibrate.select_level(res) == "L2"  # one false alarm is allowed; the gate row is not
    res["levels"]["L2"]["held_out_false_alarms"]["D6"]["k"] = 2
    assert calibrate.select_level(res) == "L3"
    res["levels"]["L3"]["held_out_false_alarms"]["D5"]["k"] = 3
    assert calibrate.select_level(res) is None


def _write_runs(directory, runs):
    directory.mkdir(parents=True, exist_ok=True)
    for i, m in enumerate(runs):
        m = dict(m)
        d2 = m.pop("d2", None)
        d1 = m.pop("d1_ok", None)
        (directory / f"run-{i:02d}.measurement.json").write_text(json.dumps(m))
        if d2 is not None:
            (directory / f"run-{i:02d}.d2.json").write_text(json.dumps({"metrics": d2}))
        if d1 is not None:
            (directory / f"run-{i:02d}.d1.json").write_text(json.dumps({"ok": d1}))


def test_cli_writes_thresholds_per_level_and_installs_the_selected_one(tmp_path):
    ref = tmp_path / "ref.json"
    ref.write_text(json.dumps(REFERENCE))
    _write_runs(tmp_path / "aa", aa_runs())
    out, installed = tmp_path / "out", tmp_path / "thresholds.json"
    args = ["--runs", str(tmp_path / "aa"), "--reference", str(ref), "--out-dir", str(out)]
    assert calibrate.main(args + ["--write-thresholds", str(installed)]) == 0
    for lv in ("L1", "L2", "L3"):
        assert (out / f"thresholds-{lv}.json").is_file()
    cal = json.loads((out / "calibration.json").read_text())
    assert cal["selected_level"] == "L1"
    assert json.loads(installed.read_text())["schema"] == score.THRESHOLDS_SCHEMA


def test_matrix_detects_faults_with_d2_only_where_it_ran(tmp_path):
    res = calibrate.calibrate(REFERENCE, aa_runs())
    thresholds = {lv: v["rules"] for lv, v in res["levels"].items()}
    faults = {
        "AA": aa_runs(10),
        "F4": [measurement(jitter=1.0, diverge=True, flip=40, d2=False) for _ in range(5)],
        "F1": [measurement(diverge=True, kld=0.3) | {"d1_ok": True} for _ in range(2)],
        "F5": [measurement(d2=False) | {"d1_ok": False} for _ in range(5)],
    }
    rows = matrix.matrix(REFERENCE, faults, thresholds)
    cell = {(r["fault"], r["level"], r["detector"]): r for r in rows}
    assert cell[("F4", "L3", "D3")]["rate"] == 1.0 and cell[("F4", "L3", "D3")]["runs"] == 5
    assert ("F4", "L3", "D2") not in cell  # D2 did not run for F4
    assert cell[("F1", "L1", "D2")]["detections"] == 2 and cell[("F1", "L1", "D2")]["runs"] == 2
    assert cell[("F5", "L3", "D1")]["rate"] == 1.0  # D1 catches F5
    assert cell[("F5", "L3", "D3")]["rate"] == 0.0  # D3 and D5 miss it
    assert cell[("F5", "L3", "D5")]["rate"] == 0.0
    assert cell[("F5", "L3", "gate")]["detections"] == 5
    assert cell[("F4", "L1", "D3")]["ci95_low"] > 0.4
    assert cell[("F4", "L2", "D3")]["d3_seconds_mean"] > 0
    assert {r["level"] for r in rows} == {"L1", "L2", "L3"}
    for name, runs in faults.items():
        _write_runs(tmp_path / name, runs)
    (tmp_path / "ref.json").write_text(json.dumps(REFERENCE))
    th = tmp_path / "th"
    th.mkdir()
    for lv, rules in thresholds.items():
        doc = {"schema": score.THRESHOLDS_SCHEMA, "rules": rules}
        (th / f"thresholds-{lv}.json").write_text(json.dumps(doc))
    out = tmp_path / "matrix.csv"
    args = ["--reference", str(tmp_path / "ref.json"), "--thresholds-dir", str(th)]
    args += ["--faults", str(tmp_path / "F4"), str(tmp_path / "F1"), "--out", str(out)]
    assert matrix.main(args) == 0
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


def test_rq2_points_come_from_d2_and_d5_of_one_run_per_model():
    sweep = {
        "Q8_0": measurement(kld=0.0),
        "Q4_K_M": measurement(kld=0.1, flip=100),
    }
    pts = rq2.points_from_runs(REFERENCE, sweep)
    assert pts[0] == {"name": "Q8_0", "mean_kld": 0.0, "accuracy_drop": 0.0}
    assert pts[1]["mean_kld"] == 0.1 and pts[1]["accuracy_drop"] > 0
