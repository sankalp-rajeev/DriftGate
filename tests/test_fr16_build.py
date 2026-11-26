"""FR-02 and FR-16 (local part): deploy/build.sh takes --commit and --run-id, records every step
in build.json, and fails the build on the first failing check (e.g. a manifest hash mismatch)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from baseline_serving import MANIFEST, serving_config, write_config

BUILD = Path("deploy/build.sh")
HEAD = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def _build(tmp_path, *args, steps="manifest", env=None):
    full_env = _clean_env(tmp_path) | {"DRIFTGATE_BUILD_STEPS": steps} | (env or {})
    tmp_path.mkdir(parents=True, exist_ok=True)
    config = write_config(tmp_path / "baseline.yaml")
    full_env.setdefault("DRIFTGATE_MANIFEST", str(MANIFEST))
    return subprocess.run(
        [str(BUILD), *args, "--config", str(config), "--out", str(tmp_path)],
        capture_output=True,
        text=True,
        env=full_env,
        timeout=600,
    )


def test_requires_commit_and_run_id(tmp_path):
    assert _build(tmp_path).returncode == 2
    assert _build(tmp_path, "--commit", HEAD).returncode == 2
    assert _build(tmp_path, "--commit", HEAD, "--run-id", "bad id!").returncode == 2


def test_wrong_commit_fails_before_any_check(tmp_path):
    res = _build(tmp_path, "--commit", "0" * 40, "--run-id", "r1")
    assert res.returncode == 1
    build = json.loads((tmp_path / "build.json").read_text())
    assert build["status"] == "failed"
    assert build["failed_step"] == "commit"
    assert [s["name"] for s in build["steps"]] == ["commit"]


def test_build_tests_step_excludes_candidate_suite(tmp_path):
    cases = tmp_path / "test_selection.py"
    cases.write_text(
        "import pytest\n\n"
        "def test_unit():\n    pass\n\n"
        "@pytest.mark.candidate_suite\n"
        "def test_candidate():\n    pytest.fail('candidate suite must not run on the build host')\n"
    )
    uv = tmp_path / "uv"
    uv.write_text(
        "#!/bin/bash\nset -eu\nshift 2\n"
        'exec "$TEST_PYTHON" -m pytest "$@" -c "$TEST_CONFIG" '
        '--confcutdir "$TEST_CASE_DIR" "$TEST_CASES"\n'
    )
    uv.chmod(0o755)
    result = _build(
        tmp_path / "build",
        "--commit",
        HEAD,
        "--run-id",
        "selection",
        steps="tests",
        env={
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "TEST_PYTHON": sys.executable,
            "TEST_CONFIG": str(Path("pyproject.toml").resolve()),
            "TEST_CASE_DIR": str(tmp_path),
            "TEST_CASES": str(cases),
            "PYTEST_ADDOPTS": "",
        },
    )
    assert result.returncode == 0, (tmp_path / "build/tests.log").read_text() + result.stderr
    assert "1 passed, 1 deselected" in (tmp_path / "build/tests.log").read_text()


def _models_present():
    from gate import manifest

    cfg = serving_config()
    return manifest.verify([cfg["model"]], MANIFEST)["ok"]


@pytest.mark.skipif(not _models_present(), reason="reference model not downloaded")
def test_manifest_step_passes_then_fails_on_hash_mismatch(tmp_path):
    ok = _build(tmp_path / "ok", "--commit", HEAD, "--run-id", "r2")
    assert ok.returncode == 0, ok.stderr
    build = json.loads((tmp_path / "ok" / "build.json").read_text())
    assert build["status"] == "passed"
    assert build["commit"] == HEAD
    assert [s["name"] for s in build["steps"]] == ["commit", "manifest"]

    bad = json.loads(MANIFEST.read_text())
    entry = bad["artifacts"]["qwen2.5-1.5b-instruct-q8_0.gguf"]
    entry["sha256"] = "0" * 64
    (tmp_path / "bad.json").write_text(json.dumps(bad))
    res = _build(
        tmp_path / "bad",
        "--commit",
        HEAD,
        "--run-id",
        "r3",
        steps="manifest d1",
        env={"DRIFTGATE_MANIFEST": str(tmp_path / "bad.json")},
    )
    assert res.returncode == 1
    build = json.loads((tmp_path / "bad" / "build.json").read_text())
    assert build["failed_step"] == "manifest"
    assert [s["name"] for s in build["steps"]] == ["commit", "manifest"]  # d1 never ran


def _clean_env(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("DRIFTGATE_")}
    # The suite also runs on the build host, where the real build.env enables S3 uploads.
    return env | {"DRIFTGATE_BUILD_ENV": str(tmp_path / "no-build.env")}


def test_package_writes_codedeploy_bundle_with_appspec_at_root(tmp_path):
    import zipfile

    env = _clean_env(tmp_path) | {"DRIFTGATE_BUILD_STEPS": "package"}
    res = subprocess.run(
        [str(BUILD), "--commit", HEAD, "--run-id", "r4", "--out", str(tmp_path)],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )
    assert res.returncode == 0, res.stderr
    bundle = tmp_path / "bundles" / f"{HEAD}.zip"  # same key as s3://<bucket>/bundles/<commit>.zip
    with zipfile.ZipFile(bundle) as z:
        names = z.namelist()
        # Bundles use the requested commit even when the working AppSpec has pending edits.
        assert z.read("appspec.yml") == subprocess.check_output(
            ["git", "show", f"{HEAD}:deploy/appspec.yml"]
        )
    assert "deploy/hooks/validate_service.sh" in names
    assert "gate/cli.py" in names
    build = json.loads((tmp_path / "build.json").read_text())
    assert [s["name"] for s in build["steps"]] == ["commit", "package"]  # no upload by default


def test_upload_is_off_by_default_and_needs_a_bucket(tmp_path):
    env = _clean_env(tmp_path) | {"DRIFTGATE_BUILD_STEPS": "", "PATH": "/usr/bin:/bin"}
    res = subprocess.run(
        [str(BUILD), "--commit", HEAD, "--run-id", "r5", "--out", str(tmp_path), "--upload"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert res.returncode == 1
    build = json.loads((tmp_path / "build.json").read_text())
    assert build["failed_step"] == "upload"
    assert "needs DRIFTGATE_BUNDLE_BUCKET" in res.stderr


@pytest.mark.parametrize("override", [False, True])
def test_ssm_build_uses_encrypted_upload_and_respects_environment(tmp_path, override):
    settings = tmp_path / "build.env"
    settings.write_text("DRIFTGATE_BUNDLE_BUCKET=lab-bucket\nDRIFTGATE_UPLOAD=1\n")
    aws = tmp_path / "aws"
    aws.write_text('#!/bin/bash\nprintf "%s\\n" "$@"\n')
    aws.chmod(0o755)
    env = _clean_env(tmp_path) | {
        "DRIFTGATE_BUILD_ENV": str(settings),
        "DRIFTGATE_BUILD_STEPS": "package",
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
    }
    if override:
        env["DRIFTGATE_BUNDLE_BUCKET"] = "override-bucket"
    result = subprocess.run(
        [str(BUILD), "--commit", HEAD, "--run-id", "r6", "--out", str(tmp_path)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    bucket = "override-bucket" if override else "lab-bucket"
    assert (tmp_path / "upload.log").read_text().splitlines() == [
        "s3",
        "cp",
        "--sse",
        "AES256",
        str(tmp_path / "bundles" / f"{HEAD}.zip"),
        f"s3://{bucket}/bundles/{HEAD}.zip",
    ]
