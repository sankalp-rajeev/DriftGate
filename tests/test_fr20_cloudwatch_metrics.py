"""FR-20 / PRD 6.8: the ValidateService hook publishes GateScore to CloudWatch only in enforce
mode and always publishes the raw per-detector values (moto CloudWatch; no real AWS)."""

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "hooks" / "publish_metrics.py"
CONFIG = json.loads((ROOT / "deploy" / "cloudwatch-metrics.json").read_text())
GROUP = "driftgate-serving"

spec = importlib.util.spec_from_file_location("publish_metrics", SCRIPT)
pm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pm)


def verdict(mode="report", gate_score=None, ttft_ratio=1.1):
    return {
        "schema": "driftgate.verdict/2",
        "detectors": {
            "D3": {"mean_agreement_share": 0.97, "mean_approx_kl": 0.01, "p95_approx_kl": 0.05},
            "D5": {"delta_accuracy": -0.02, "accuracy_drop": 0.02},
            "D6": {
                "candidate": {"p95_ttft_ms": 210.0, "p95_itl_ms": 21.0},
                "ratio": {"p95_ttft_ms": ttft_ratio, "p95_itl_ms": 1.05},
            },
            "D7": {
                "mean_similarity": 0.98,
                "p5_similarity": 0.95,
                "semantic_drift": 0.05,
                "embedding_seconds": 1.2,
            },
        },
        "decision": {"mode": mode, "gate_score": gate_score, "exit_code": 0},
    }


RAW = [m["name"] for m in CONFIG["raw"]]


def names(data):
    return [d["MetricName"] for d in data]


def test_report_mode_publishes_raw_values_only():
    data = pm.metric_data(verdict("report"), CONFIG, GROUP)
    assert names(data) == RAW
    assert "GateScore" not in names(data)


def test_enforce_mode_publishes_gate_score_and_raw_values():
    data = pm.metric_data(verdict("enforce", gate_score=1.4), CONFIG, GROUP)
    assert names(data) == ["GateScore", *RAW]
    assert data[0]["Value"] == 1.4
    assert data[0]["Dimensions"] == [{"Name": "DeploymentGroup", "Value": GROUP}]


def test_early_stop_publishes_gate_score_and_measured_metrics():
    v = verdict("enforce", gate_score=2.0)
    v["early_stop"] = True
    v["detectors"]["D5"] = {"status": "not run", "reason": "early stop"}
    data = pm.metric_data(v, CONFIG, GROUP)
    assert data[0]["MetricName"] == "GateScore" and data[0]["Value"] == 2.0
    assert any(name.startswith("D3") for name in names(data))
    assert any(name.startswith("D6") for name in names(data))
    assert not any(name.startswith("D5") for name in names(data))


def test_report_mode_never_publishes_gate_score_even_if_present():
    assert "GateScore" not in names(pm.metric_data(verdict("report", 9.0), CONFIG, GROUP))


def test_missing_or_non_finite_values_are_skipped():
    v = verdict("enforce", gate_score=None, ttft_ratio=None)
    v["detectors"].pop("D5")
    got = names(pm.metric_data(v, CONFIG, GROUP))
    assert "GateScore" not in got and "D6TtftRatio" not in got
    assert not any(n.startswith("D5") for n in got)
    v2 = verdict("enforce", gate_score=float("inf"))
    assert "GateScore" not in names(pm.metric_data(v2, CONFIG, GROUP))


def test_raw_metrics_cover_d3_d5_d6_d7():
    assert {m["detector"] for m in CONFIG["raw"]} == {"D3", "D5", "D6", "D7"}
    # D6 is published as a ratio to the reference p95 (PRD 6.8), plus the raw milliseconds.
    assert {"ratio.p95_ttft_ms", "ratio.p95_itl_ms"} <= {m["path"] for m in CONFIG["raw"]}


def test_publish_to_moto_cloudwatch(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    path = tmp_path / "verdict.json"
    path.write_text(json.dumps(verdict("enforce", gate_score=1.2)))
    with mock_aws():
        cw = boto3.client("cloudwatch", region_name="us-east-1")
        assert pm.main(["--verdict", str(path), "--deployment-group", GROUP], cloudwatch=cw) == 0
        listed = cw.list_metrics(Namespace="DriftGate")["Metrics"]
        assert {m["MetricName"] for m in listed} == {"GateScore", *RAW}
        assert all(m["Dimensions"] == [{"Name": "DeploymentGroup", "Value": GROUP}] for m in listed)


def test_metric_names_match_the_alarm_and_dashboard():
    from cfnlint.decode import decode

    tpl, _ = decode(str(ROOT / "deploy" / "cloudformation" / "codedeploy.yaml"))
    alarm = tpl["Resources"]["GateScoreAlarm"]["Properties"]
    assert alarm["Namespace"] == CONFIG["namespace"]
    assert alarm["MetricName"] == CONFIG["gate_score"]["name"]
    assert alarm["Dimensions"][0]["Name"] == CONFIG["dimension"]


@pytest.mark.parametrize("gate_exit", [0, 1])
def test_hook_publish_failure_does_not_change_hook_exit(tmp_path, baseline_config, gate_exit):
    """The hook calls the publisher; a CloudWatch failure is logged and the gate decides."""
    from fake_llama import FakeLlama

    from gate import cli

    gate_cfg = json.loads(json.dumps(__import__("yaml").safe_load(
        (ROOT / "gate" / "config.yaml").read_text())))  # fmt: skip
    gate_cfg["reference"] = str(tmp_path / "reference.json")
    gate_cfg["thresholds"] = str(tmp_path / "thresholds.json")
    cfg_path = tmp_path / "gate.yaml"
    cfg_path.write_text(json.dumps(gate_cfg))
    with FakeLlama() as server:
        assert cli.main(["measure", "--config", str(baseline_config),
                         "--gate-config", str(cfg_path), "--server",
                         f"http://127.0.0.1:{server.port}", "--level", "L1", "--seed", "0",
                         "--out", gate_cfg["reference"]]) == 0  # fmt: skip
        env = {k: v for k, v in os.environ.items() if not k.startswith("DRIFTGATE_")}
        env.pop("PYTHONPATH", None)
        env.update(
            DRIFTGATE_INSTANCE_ENV=str(tmp_path / "none.env"),
            DRIFTGATE_APP_DIR=str(ROOT),
            DRIFTGATE_GATE_CONFIG=str(cfg_path),
            DRIFTGATE_SERVING_CONFIG=str(baseline_config),
            DRIFTGATE_SERVING_PORT=str(server.port),
            DRIFTGATE_RUN_DIR=str(tmp_path / "runs"),
            DRIFTGATE_HEALTH_TIMEOUT_S="4",
            DRIFTGATE_SYSTEMD="0",
            DRIFTGATE_BUCKET="",
            DRIFTGATE_PUBLISH_METRICS="1",
            DRIFTGATE_FORCE_FAIL=str(gate_exit),
            DEPLOYMENT_ID="d-TEST",
            # Never reach AWS: CloudWatch points at a closed local port, one attempt.
            AWS_ENDPOINT_URL_CLOUDWATCH="http://127.0.0.1:9",
            AWS_ACCESS_KEY_ID="testing",
            AWS_SECRET_ACCESS_KEY="testing",
            AWS_MAX_ATTEMPTS="1",
        )
        r = subprocess.run(["bash", str(ROOT / "deploy" / "hooks" / "validate_service.sh")],
                           env=env, capture_output=True, text=True, timeout=120)  # fmt: skip
    assert r.returncode == gate_exit, r.stderr
    assert "CloudWatch metric publish failed" in r.stderr
