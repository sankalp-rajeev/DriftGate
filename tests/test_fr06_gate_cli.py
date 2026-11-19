"""Gate CLI (FR-06, FR-08, FR-09, PRD Section 6.8): versioned JSON verdicts, gate mode, force-fail.

Measurement only: no thresholds file is committed; tests use temporary ones to exercise enforce.
"""

import json
import math
from pathlib import Path

import pytest

from gate import cli
from gate.llama_client import LlamaServer
from gate.manifest import load_yaml

GATE_CONFIG = load_yaml(Path("gate/config.yaml"))
VERDICT_KEYS = {
    "schema",
    "budget",
    "seed",
    "reference",
    "candidate",
    "detectors",
    "compare_wall_clock_s",
    "decision",
}
DECISION_KEYS = {
    "mode",
    "configured_mode",
    "thresholds_file",
    "rules",
    "scores",
    "gate_score",
    "passed",
    "forced_fail",
    "exit_code",
}
D3_KEYS = {"n_prompts", "mean_agreement_share", "mean_approx_kl", "p95_approx_kl", "per_prompt"}
D5_KEYS = {
    "n_items",
    "reference_accuracy",
    "candidate_accuracy",
    "delta_accuracy",
    "accuracy_drop",
    "ci95",
    "bootstrap",
    "mcnemar",
}
D6_KEYS = {"n_requests", "reference", "candidate", "delta_ms", "ratio"}


def _measurement(tokens, correct, prompt_ms=50.0):
    return {
        "schema": cli.MEASUREMENT_SCHEMA,
        "budget": len(tokens),
        "seed": 0,
        "server": {"url": "http://x", "model_path": "m.gguf", "build_info": "b1"},
        "inputs": {
            "config": None,
            "config_sha256": None,
            "d3_prompts": "p",
            "d3_prompts_sha256": "p",
            "d5_panel": "q",
            "d5_panel_sha256": "q",
        },
        "wall_clock_s": {"D3": 1.0, "D5": 1.0},
        "d3": [
            {
                "id": i,
                "tokens": t,
                "top": [[[x, math.log(0.9)], [x + 1, math.log(0.1)]] for x in t],
                "timings": {
                    "prompt_ms": prompt_ms,
                    "predicted_ms": 64.0,
                    "prompt_n": 5,
                    "predicted_n": len(t),
                },
            }
            for i, t in enumerate(tokens)
        ],
        "d5": [{"id": f"s/{i}", "predicted": "A", "correct": c} for i, c in enumerate(correct)],
    }


REF = _measurement([[1, 2, 3], [4, 5, 6]], [True, False])
CAND = _measurement([[1, 2, 3], [4, 7, 8]], [False, False], prompt_ms=80.0)


def _check_schema(v):
    assert set(v) == VERDICT_KEYS
    assert v["schema"] == "driftgate.verdict/2"
    assert set(v["detectors"]) == {"D3", "D5", "D6"}
    assert set(v["detectors"]["D3"]) == D3_KEYS
    assert set(v["detectors"]["D5"]) == D5_KEYS
    assert set(v["detectors"]["D6"]) == D6_KEYS
    assert set(v["decision"]) == DECISION_KEYS


def _files(tmp_path, mode="auto", thresholds=None):
    (tmp_path / "ref.json").write_text(json.dumps(REF))
    (tmp_path / "cand.json").write_text(json.dumps(CAND))
    tpath = tmp_path / "thresholds.json"
    if thresholds is not None:
        tpath.write_text(json.dumps(thresholds))
    cfg = dict(GATE_CONFIG, mode=mode, thresholds=str(tpath), reference=str(tmp_path / "ref.json"))
    (tmp_path / "gate.yaml").write_text(json.dumps(cfg))  # JSON is valid YAML
    return ["compare", "--gate-config", str(tmp_path / "gate.yaml")] + [
        "--candidate",
        str(tmp_path / "cand.json"),
        "--out",
        str(tmp_path / "verdict.json"),
    ]


def _thresholds(value):
    rule = {"detector": "D3", "metric": "mean_agreement_share", "kind": "agreement", "value": value}
    return {"schema": cli.THRESHOLDS_SCHEMA, "rules": [rule]}


def test_committed_config_has_no_thresholds_and_runs_in_report_mode():
    assert GATE_CONFIG["mode"] == "auto"
    assert not Path(GATE_CONFIG["thresholds"]).exists()
    assert cli.resolve_mode("auto", Path(GATE_CONFIG["thresholds"])) == "report"


def test_report_mode_records_verdict_and_exits_zero(tmp_path, monkeypatch):
    monkeypatch.delenv(cli.FORCE_FAIL_ENV, raising=False)
    assert cli.main(_files(tmp_path)) == 0
    v = json.loads((tmp_path / "verdict.json").read_text())
    _check_schema(v)
    assert v["decision"]["mode"] == "report"
    assert v["decision"]["rules"] is None
    assert v["decision"]["passed"] is None
    assert v["detectors"]["D3"]["mean_agreement_share"] < 1
    assert v["detectors"]["D5"]["delta_accuracy"] == -0.5
    assert v["detectors"]["D6"]["delta_ms"]["p95_ttft_ms"] == 30.0


def test_auto_switches_to_enforce_when_thresholds_exist(tmp_path, monkeypatch):
    monkeypatch.delenv(cli.FORCE_FAIL_ENV, raising=False)
    # Placeholder rule values chosen only to exercise pass and fail; not calibrated thresholds.
    assert cli.main(_files(tmp_path, thresholds=_thresholds(0.0))) == 0
    v = json.loads((tmp_path / "verdict.json").read_text())
    assert v["decision"]["mode"] == "enforce"
    assert v["decision"]["passed"] is True
    assert v["decision"]["gate_score"] == pytest.approx((1 - 2 / 3) / 1)
    assert cli.main(_files(tmp_path, thresholds=_thresholds(0.9))) == 1
    v = json.loads((tmp_path / "verdict.json").read_text())
    assert v["decision"]["passed"] is False
    assert v["decision"]["scores"] == {"D3": pytest.approx((1 - 2 / 3) / 0.1)}
    assert v["decision"]["gate_score"] > 1
    assert v["decision"]["rules"][0]["ok"] is False


def test_enforce_without_thresholds_file_is_an_error(tmp_path, monkeypatch):
    monkeypatch.delenv(cli.FORCE_FAIL_ENV, raising=False)
    assert cli.main(_files(tmp_path, mode="enforce")) == 2


@pytest.mark.parametrize(("mode", "thresholds"), [("report", None), ("auto", 0.0)])
def test_force_fail_env_exits_nonzero_in_either_mode(tmp_path, monkeypatch, mode, thresholds):
    monkeypatch.setenv(cli.FORCE_FAIL_ENV, "1")
    t = None if thresholds is None else _thresholds(thresholds)
    assert cli.main(_files(tmp_path, mode=mode, thresholds=t)) == 1
    v = json.loads((tmp_path / "verdict.json").read_text())
    assert v["decision"]["forced_fail"] is True


def test_compare_refuses_mismatched_data():
    cand = json.loads(json.dumps(CAND))
    cand["inputs"]["d5_panel_sha256"] = "other"
    with pytest.raises(ValueError, match="d5_panel_sha256"):
        cli.detectors(REF, cand)


def test_live_aa_on_ci_model_is_identical(ci_server):
    """Two greedy measurements of the same server agree exactly (A/A, budget 4)."""
    server = LlamaServer(ci_server)
    data = GATE_CONFIG["data"]
    paths = (Path(data["d3_prompts"]), Path(data["d5_panel"]))
    a = cli.measure(server, 4, 0, *paths)
    b = cli.measure(server, 4, 0, *paths)
    v = cli.verdict(a, b, dict(GATE_CONFIG, thresholds="/nonexistent"), env={})
    _check_schema(v)
    assert len(a["d3"][0]["top"][0]) == 10  # n_probs 10
    assert all(len(r["tokens"]) <= 32 for r in a["d3"])
    assert v["detectors"]["D3"]["mean_agreement_share"] == 1.0
    assert v["detectors"]["D3"]["mean_approx_kl"] < 1e-6
    assert v["detectors"]["D5"]["delta_accuracy"] == 0.0
    assert v["decision"]["exit_code"] == 0
