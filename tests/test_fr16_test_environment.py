"""Run pytest under Lab host settings and exercise real build/hook env loading."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_suite_ignores_host_settings_but_allows_explicit_test_configuration(tmp_path):
    host = tmp_path / "etc/driftgate"
    host.mkdir(parents=True)
    build_env = host / "build.env"
    build_env.write_text("DRIFTGATE_UPLOAD=1\nDRIFTGATE_BUNDLE_BUCKET=host-bucket\n")
    instance_env = host / "deploy.env"
    instance_env.write_text("DRIFTGATE_FORCE_FAIL=1\nDRIFTGATE_BUCKET=host-bucket\n")
    commands = tmp_path / "commands"
    commands.mkdir()
    aws = commands / "aws"
    aws.write_text('#!/bin/bash\nprintf "%s\\n" "$@"\n')
    aws.chmod(0o755)

    probe = tmp_path / "suite"
    probe.mkdir()
    for name in ("conftest.py", "orchestrator_helpers.py", "llama_fixtures.py"):
        shutil.copy(ROOT / "tests" / name, probe / name)
    (probe / "test_host_env.py").write_text("""
import json
import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("explicit", [False, True])
def test_host_settings(tmp_path, monkeypatch, explicit):
    root = Path(os.environ["TEST_REPO"])
    if explicit:
        monkeypatch.setenv("DRIFTGATE_BUILD_ENV", os.environ["TEST_BUILD_ENV"])
        monkeypatch.setenv("DRIFTGATE_INSTANCE_ENV", os.environ["TEST_INSTANCE_ENV"])
        for key, value in {
            "DRIFTGATE_UPLOAD": "1", "DRIFTGATE_BUCKET": "test-bucket",
            "DRIFTGATE_BUNDLE_BUCKET": "test-bucket", "DRIFTGATE_FORCE_FAIL": "1",
        }.items():
            monkeypatch.setenv(key, value)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    result = subprocess.run(
        [str(root / "deploy/build.sh"), "--commit", commit, "--run-id", "hermetic",
         "--out", str(tmp_path)],
        env=os.environ | {"DRIFTGATE_BUILD_STEPS": ""}, cwd=root,
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads((tmp_path / "build.json").read_text())
    assert [step["name"] for step in report["steps"]] == (
        ["commit", "upload"] if explicit else ["commit"]
    )
    if explicit:
        assert "s3://test-bucket/bundles/" in (tmp_path / "upload.log").read_text()
    else:
        for key in ("DRIFTGATE_BUILD_ENV", "DRIFTGATE_INSTANCE_ENV"):
            path = Path(os.environ[key])
            assert path.is_relative_to(tmp_path)
            assert not path.exists()
        for key in ("DRIFTGATE_UPLOAD", "DRIFTGATE_BUCKET", "DRIFTGATE_BUNDLE_BUCKET",
                    "DRIFTGATE_FORCE_FAIL"):
            assert key not in os.environ
    hook = subprocess.run(
        ["bash", "-c", 'source deploy/hooks/lib.sh; '
         'printf "%s|%s" "$DRIFTGATE_FORCE_FAIL" "$DRIFTGATE_BUCKET"'],
        cwd=root, capture_output=True, text=True, timeout=10,
    )
    assert hook.returncode == 0, hook.stderr
    assert hook.stdout == ("1|test-bucket" if explicit else "0|")
""")
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("PYTHONPATH", "BASH_ENV", "PYTEST_ADDOPTS")
    } | {
        "PATH": f"{commands}:{os.environ['PATH']}",
        "TEST_REPO": str(ROOT),
        "TEST_BUILD_ENV": str(build_env),
        "TEST_INSTANCE_ENV": str(instance_env),
        "DRIFTGATE_BUILD_ENV": str(build_env),
        "DRIFTGATE_INSTANCE_ENV": str(instance_env),
        "DRIFTGATE_UPLOAD": "1",
        "DRIFTGATE_BUCKET": "host-bucket",
        "DRIFTGATE_BUNDLE_BUCKET": "host-bucket",
        "DRIFTGATE_FORCE_FAIL": "1",
    }
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--confcutdir", str(probe), str(probe)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout
