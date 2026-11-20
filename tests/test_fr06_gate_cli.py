"""Gate CLI (FR-06, FR-08, FR-09, PRD Section 6.8): versioned JSON verdicts, gate mode, force-fail.

Measurement only: no thresholds file is committed; tests use temporary ones to exercise enforce.
"""

import json
import math
from pathlib import Path

import pytest
from fake_llama import FakeLlama

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


def test_http_error_is_a_detector_detection_and_preserves_other_measurements(tmp_path):
    data = GATE_CONFIG["data"]
    paths = (Path(data["d3_prompts"]), Path(data["d5_panel"]))
    with FakeLlama() as healthy:
        reference = cli.measure(LlamaServer(f"http://127.0.0.1:{healthy.port}"), 2, 0, *paths)
    with FakeLlama(errors={1: (400, "prompt exceeds context size 256")}) as broken:
        candidate = cli.measure(LlamaServer(f"http://127.0.0.1:{broken.port}"), 2, 0, *paths)
    verdict = cli.verdict(
        reference, candidate, dict(GATE_CONFIG, thresholds=str(tmp_path / "none"))
    )
    assert len(candidate["d3"]) == 2
    assert verdict["detectors"]["D5"]["status"] == "error"
    assert verdict["detectors"]["D5"]["error"]["status_code"] == 400
    assert "context size 256" in verdict["detectors"]["D5"]["error"]["message"]
    assert verdict["detectors"]["D3"]["mean_agreement_share"] == 1.0
    assert verdict["decision"]["exit_code"] == 1
    assert verdict["decision"]["passed"] is False


@pytest.mark.parametrize("error_kind", ["http", "timeout"])
def test_d3_request_error_also_marks_d6_and_keeps_d5_running(tmp_path, error_kind):
    data = GATE_CONFIG["data"]
    paths = (Path(data["d3_prompts"]), Path(data["d5_panel"]))
    with FakeLlama() as healthy:
        reference = cli.measure(LlamaServer(f"http://127.0.0.1:{healthy.port}"), 2, 0, *paths)
    kwargs = (
        {"errors": {32: (503, "temporarily unavailable")}}
        if error_kind == "http"
        else {"delays": {32: 0.2}}
    )
    with FakeLlama(**kwargs) as broken:
        candidate = cli.measure(
            LlamaServer(f"http://127.0.0.1:{broken.port}", timeout=0.05), 2, 0, *paths
        )
    verdict = cli.verdict(
        reference, candidate, dict(GATE_CONFIG, thresholds=str(tmp_path / "none"))
    )
    for det in ("D3", "D6"):
        result = verdict["detectors"][det]
        assert result["status"] == "error"
        assert result["error"]["kind"] == error_kind
        assert result["error"]["status_code"] == (503 if error_kind == "http" else None)
        assert result["error"]["message"]
    assert verdict["detectors"]["D5"]["n_items"] == 2
    assert verdict["decision"]["exit_code"] == 1
    json.dumps(candidate, allow_nan=False)
    json.dumps(verdict, allow_nan=False)


@pytest.mark.parametrize("failed_request", [0, 1])
def test_gate_run_persists_http_errors_and_blocks_candidate(tmp_path, failed_request):
    data = GATE_CONFIG["data"]
    paths = (Path(data["d3_prompts"]), Path(data["d5_panel"]))
    with FakeLlama() as healthy:
        reference = cli.measure(LlamaServer(f"http://127.0.0.1:{healthy.port}"), 2, 0, *paths)
    (tmp_path / "reference.json").write_text(json.dumps(reference))
    cfg = dict(
        GATE_CONFIG, reference=str(tmp_path / "reference.json"), thresholds=str(tmp_path / "none")
    )
    (tmp_path / "gate.yaml").write_text(json.dumps(cfg))
    measurement, verdict = tmp_path / "measurement.json", tmp_path / "verdict.json"
    with FakeLlama(errors={failed_request: (400, "context too small")}) as broken:
        assert (
            cli.main(
                [
                    "run",
                    "--gate-config",
                    str(tmp_path / "gate.yaml"),
                    "--server",
                    f"http://127.0.0.1:{broken.port}",
                    "--seed",
                    "0",
                    "--budget",
                    "2",
                    "--out",
                    str(verdict),
                    "--measurement-out",
                    str(measurement),
                ]
            )
            == 1
        )
    m, v = json.loads(measurement.read_text()), json.loads(verdict.read_text())
    assert m["d5"][-1]["error"]["status_code"] == 400
    assert v["detectors"]["D5"]["status"] == "error"
    if failed_request == 0:
        assert v["detectors"]["D3"]["status"] == v["detectors"]["D6"]["status"] == "error"


def test_reference_request_errors_are_never_saved_as_baseline(tmp_path):
    cfg = dict(
        GATE_CONFIG, reference_store=dict(GATE_CONFIG["reference_store"], local_root=str(tmp_path))
    )
    (tmp_path / "gate.yaml").write_text(json.dumps(cfg))
    with FakeLlama(errors={1: (400, "context too small")}) as broken:
        assert (
            cli.main(
                [
                    "reference",
                    "--gate-config",
                    str(tmp_path / "gate.yaml"),
                    "--server",
                    f"http://127.0.0.1:{broken.port}",
                    "--seed",
                    "0",
                    "--budget",
                    "2",
                    "--instance-type",
                    "local-test",
                ]
            )
            == 2
        )
    assert not list(tmp_path.glob("reference/**/measurement.json"))


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
