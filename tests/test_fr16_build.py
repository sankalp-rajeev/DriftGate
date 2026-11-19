"""FR-02 and FR-16 (local part): deploy/build.sh takes --commit and --run-id, records every step
in build.json, and fails the build on the first failing check (e.g. a manifest hash mismatch)."""

import json
import os
import subprocess
from pathlib import Path

import pytest

BUILD = Path("deploy/build.sh")
HEAD = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def _build(tmp_path, *args, steps="manifest", env=None):
    full_env = dict(os.environ, DRIFTGATE_BUILD_STEPS=steps, **(env or {}))
    return subprocess.run(
        [str(BUILD), *args, "--out", str(tmp_path)],
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


def _models_present():
    from gate import manifest

    cfg = manifest.load_yaml(Path("serving/config.yaml"))
    return manifest.verify([cfg["model"]])["ok"]


@pytest.mark.skipif(not _models_present(), reason="reference model not downloaded")
def test_manifest_step_passes_then_fails_on_hash_mismatch(tmp_path):
    ok = _build(tmp_path / "ok", "--commit", HEAD, "--run-id", "r2")
    assert ok.returncode == 0, ok.stderr
    build = json.loads((tmp_path / "ok" / "build.json").read_text())
    assert build["status"] == "passed"
    assert build["commit"] == HEAD
    assert [s["name"] for s in build["steps"]] == ["commit", "manifest"]

    bad = json.loads(Path("serving/manifest.json").read_text())
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
