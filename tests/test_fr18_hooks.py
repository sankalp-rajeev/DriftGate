"""FR-18: appspec.yml hooks; ValidateService runs D3, D5, D6 through the gate CLI and exits
non-zero on gate failure. PRD 6.8: report mode until gate/thresholds.json exists, then enforce;
DRIFTGATE_FORCE_FAIL=1 fails the hook in either mode.

The hooks run here against a fake llama-server (tests/fake_llama.py), with the app directory set
to this checkout. No thresholds file is created in the repo; enforce uses a temporary one.
"""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml
from fake_llama import FakeLlama

from gate import cli

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "deploy" / "hooks"
APPSPEC = ROOT / "deploy" / "appspec.yml"
EXPECTED_HOOKS = ["ApplicationStop", "BeforeInstall", "AfterInstall", "ApplicationStart",
                  "ValidateService"]  # fmt: skip
BUDGET = 4


def load_appspec():
    return yaml.safe_load(APPSPEC.read_text())


def test_appspec_has_the_five_hooks_in_order():
    spec = load_appspec()
    assert spec["version"] == 0.0
    assert spec["os"] == "linux"
    assert list(spec["hooks"]) == EXPECTED_HOOKS


def test_appspec_hook_scripts_exist_and_are_executable():
    for name, entries in load_appspec()["hooks"].items():
        [entry] = entries
        path = ROOT / entry["location"]
        assert path.is_file(), name
        assert path.stat().st_mode & stat.S_IXUSR, f"{path} not executable"
        assert path.read_text().startswith("#!/usr/bin/env bash")
        assert entry["runas"] == "root"


def test_validate_service_timeout_within_nfr01_kill_limit():
    [entry] = load_appspec()["hooks"]["ValidateService"]
    assert entry["timeout"] <= 15 * 60


def test_appspec_installs_to_the_configured_app_dir():
    spec = load_appspec()
    env = dict(
        line.split("=", 1)
        for line in (ROOT / "deploy" / "config.env").read_text().splitlines()
        if line and not line.startswith("#")
    )
    assert spec["files"] == [{"source": "/", "destination": env["DRIFTGATE_APP_DIR"]}]
    assert spec["file_exists_behavior"] == "OVERWRITE"
    assert env["DRIFTGATE_SERVING_PORT"] == "8080"


def test_hook_scripts_parse():
    for path in HOOKS.glob("*.sh"):
        subprocess.run(["bash", "-n", str(path)], check=True)


def test_no_thresholds_file_committed():
    assert not (ROOT / "gate" / "thresholds.json").exists()


@pytest.fixture
def sandbox(tmp_path):
    """Gate config pointing at temporary reference/thresholds paths, and a hook environment."""
    gate_cfg = yaml.safe_load((ROOT / "gate" / "config.yaml").read_text())
    gate_cfg["reference"] = str(tmp_path / "reference.json")
    gate_cfg["thresholds"] = str(tmp_path / "thresholds.json")
    gate_cfg_path = tmp_path / "gate.yaml"
    gate_cfg_path.write_text(yaml.safe_dump(gate_cfg))
    run_dir = tmp_path / "runs"

    def env(port, **extra):
        e = {k: v for k, v in os.environ.items() if not k.startswith("DRIFTGATE_")}
        e.pop("PYTHONPATH", None)
        e.update(
            DRIFTGATE_INSTANCE_ENV=str(tmp_path / "no-instance.env"),
            DRIFTGATE_APP_DIR=str(ROOT),
            DRIFTGATE_GATE_CONFIG=str(gate_cfg_path),
            DRIFTGATE_GATE_BUDGET=str(BUDGET),
            DRIFTGATE_SERVING_PORT=str(port),
            DRIFTGATE_RUN_DIR=str(run_dir),
            DRIFTGATE_HEALTH_TIMEOUT_S="4",
            DRIFTGATE_SYSTEMD="0",
            DRIFTGATE_PUBLISH_METRICS="0",
            DRIFTGATE_BUCKET="",
            DEPLOYMENT_ID="d-TEST",
            LIFECYCLE_EVENT="ValidateService",
        )
        e.update(extra)
        return e

    def measure_reference(port):
        rc = cli.main(
            ["measure", "--gate-config", str(gate_cfg_path), "--server",
             f"http://127.0.0.1:{port}", "--budget", str(BUDGET), "--seed", "0",
             "--out", gate_cfg["reference"]]
        )  # fmt: skip
        assert rc == 0

    return {
        "env": env,
        "measure_reference": measure_reference,
        "thresholds": Path(gate_cfg["thresholds"]),
        "verdict": run_dir / "d-TEST" / "verdict.json",
    }


def run_hook(name, env):
    return subprocess.run(
        ["bash", str(HOOKS / name)], env=env, capture_output=True, text=True, timeout=120
    )


def write_thresholds(path):
    # Placeholder value that only exercises enforce (not a calibrated threshold); schema v2.
    rule = {"detector": "D3", "metric": "mean_agreement_share", "kind": "agreement", "value": 0.99}
    path.write_text(json.dumps({"schema": cli.THRESHOLDS_SCHEMA, "rules": [rule]}))


def test_validate_service_report_mode_records_verdict_and_passes(sandbox):
    with FakeLlama() as ref:
        sandbox["measure_reference"](ref.port)
    with FakeLlama(shift=7) as drifted:
        r = run_hook("validate_service.sh", sandbox["env"](drifted.port))
    assert r.returncode == 0, r.stderr
    verdict = json.loads(sandbox["verdict"].read_text())
    assert verdict["schema"] == cli.VERDICT_SCHEMA
    assert set(verdict["detectors"]) == {"D3", "D5", "D6"}
    assert verdict["decision"]["mode"] == "report"
    assert verdict["detectors"]["D3"]["mean_agreement_share"] < 1.0
    assert (sandbox["verdict"].parent / "measurement.json").exists()


def test_validate_service_enforce_fails_on_gate_failure(sandbox):
    with FakeLlama() as ref:
        sandbox["measure_reference"](ref.port)
    write_thresholds(sandbox["thresholds"])
    with FakeLlama(shift=7) as drifted:
        r = run_hook("validate_service.sh", sandbox["env"](drifted.port))
    assert r.returncode == 1, r.stderr
    decision = json.loads(sandbox["verdict"].read_text())["decision"]
    assert decision["mode"] == "enforce"
    assert decision["passed"] is False


def test_validate_service_enforce_passes_on_identical_behaviour(sandbox):
    with FakeLlama() as server:
        sandbox["measure_reference"](server.port)
        write_thresholds(sandbox["thresholds"])
        r = run_hook("validate_service.sh", sandbox["env"](server.port))
    assert r.returncode == 0, r.stderr
    assert json.loads(sandbox["verdict"].read_text())["decision"]["passed"] is True


@pytest.mark.parametrize("enforce", [False, True])
def test_force_fail_fails_validate_service_in_either_mode(sandbox, enforce):
    with FakeLlama() as server:
        sandbox["measure_reference"](server.port)
        if enforce:
            write_thresholds(sandbox["thresholds"])
        r = run_hook("validate_service.sh", sandbox["env"](server.port, DRIFTGATE_FORCE_FAIL="1"))
    assert r.returncode != 0
    assert json.loads(sandbox["verdict"].read_text())["decision"]["forced_fail"] is True


def test_force_fail_from_instance_env_file(sandbox, tmp_path):
    """Drills can set the flag in /etc/driftgate/deploy.env without a new revision."""
    inst = tmp_path / "instance.env"
    inst.write_text("DRIFTGATE_FORCE_FAIL=1\n")
    with FakeLlama() as server:
        sandbox["measure_reference"](server.port)
        env = sandbox["env"](server.port, DRIFTGATE_INSTANCE_ENV=str(inst))
        env.pop("DRIFTGATE_FORCE_FAIL", None)
        r = run_hook("validate_service.sh", env)
    assert r.returncode == 1


def test_validate_service_fails_when_server_is_down(sandbox):
    with FakeLlama() as server:
        port = server.port
    r = run_hook("validate_service.sh", sandbox["env"](port))
    assert r.returncode == 3


def test_validate_service_fails_without_reference(sandbox):
    with FakeLlama() as server:
        r = run_hook("validate_service.sh", sandbox["env"](server.port))
    assert r.returncode == 2


def test_application_start_waits_for_health(sandbox):
    with FakeLlama() as server:
        r = run_hook("application_start.sh", sandbox["env"](server.port))
    assert r.returncode == 0, r.stderr
    assert "healthy" in r.stderr


def test_application_stop_and_before_install_are_safe_locally(sandbox, tmp_path):
    env = sandbox["env"](
        1,
        DRIFTGATE_APP_DIR=str(tmp_path / "app"),
        DRIFTGATE_INSTANCE_SERVING_CONFIG=str(tmp_path / "etc" / "serving.yaml"),
    )
    assert run_hook("application_stop.sh", env).returncode == 0
    assert run_hook("before_install.sh", env).returncode == 0
    assert (tmp_path / "app").is_dir() and (tmp_path / "etc").is_dir()
