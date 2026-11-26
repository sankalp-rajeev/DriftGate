"""Enforced runs stop after a decisive D3/D6 result; report runs still measure D5."""

import json
from pathlib import Path

import pytest
from baseline_serving import run_gate
from fake_llama import FakeLlama

from gate import cli
from gate.manifest import load_yaml


class Candidate(FakeLlama):
    def __init__(self, fault=None):
        super().__init__(shift=7 if fault == "D3" else 0)
        self.fault = fault
        self.requests = []

    def completion(self, request):
        self.requests.append(request["n_predict"])
        result = super().completion(request)
        if self.fault == "D6":
            result["timings"]["predicted_ms"] *= 2
        return result


@pytest.mark.parametrize(
    ("mode", "fault", "early"),
    [
        ("enforce", "D6", True),
        ("enforce", "D3", True),
        ("auto", "D6", True),
        ("enforce", None, False),
        ("report", "D6", False),
        ("report", "D3", False),
    ],
)
def test_run_stops_only_after_an_enforced_d3_or_d6_failure(tmp_path, mode, fault, early):
    config = load_yaml(Path("gate/config.yaml"))
    data = config["data"]
    with FakeLlama() as baseline:
        reference = cli.measure(
            cli.LlamaServer(f"http://127.0.0.1:{baseline.port}"),
            2,
            0,
            Path(data["d3_prompts"]),
            Path(data["d5_panel"]),
        )
    reference_path = tmp_path / "reference.json"
    reference_path.write_text(json.dumps(reference))
    config |= {
        "mode": mode,
        "reference": str(reference_path),
        "thresholds": str(Path("gate/thresholds.json").resolve()),
    }
    gate_config = tmp_path / "gate.json"
    gate_config.write_text(json.dumps(config))
    verdict_path, measurement_path = tmp_path / "verdict.json", tmp_path / "measurement.json"
    with Candidate(fault) as candidate:
        result = run_gate(
            [
                "run",
                "--gate-config",
                str(gate_config),
                "--server",
                f"http://127.0.0.1:{candidate.port}",
                "--budget",
                "2",
                "--seed",
                "0",
                "--out",
                str(verdict_path),
                "--measurement-out",
                str(measurement_path),
            ]
        )
    assert result == (1 if mode != "report" and fault else 0)
    verdict = json.loads(verdict_path.read_text())
    measurement = json.loads(measurement_path.read_text())
    assert verdict.get("early_stop", False) is early
    assert candidate.requests == ([32, 32] if early else [32, 32, 1, 1])
    if early:
        assert verdict["detectors"]["D5"] == {"status": "not run", "reason": "early stop"}
        assert measurement["d5"] == []
        assert verdict["decision"]["scores"][fault] > 1
        assert "D5" not in verdict["decision"]["scores"]
    else:
        assert len(measurement["d5"]) == 2
        assert verdict["detectors"]["D5"]["n_items"] == 2
