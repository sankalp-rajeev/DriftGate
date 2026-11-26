"""A serving fault reaches the detectors without failing baseline unit-test assumptions."""

import json
import os
import shutil
import subprocess
import sys

import pytest
from baseline_serving import ROOT, workspace

from faults import inject


def copy_candidate(destination):
    subprocess.run(["git", "clone", "--shared", "--quiet", str(ROOT), str(destination)], check=True)
    files = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT, text=True
    ).splitlines()
    for name in files:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if (ROOT / name).is_symlink():
            target.unlink(missing_ok=True)
            target.symlink_to(os.readlink(ROOT / name))
        else:
            shutil.copy2(ROOT / name, target)
    for name in (".venv", "models", "artifacts"):
        if (ROOT / name).exists():
            (destination / name).symlink_to(ROOT / name, target_is_directory=True)


@pytest.mark.parametrize("fault", ["F1", "F4", "F8", "F9"])
def test_full_suite_accepts_candidate_serving_changes(tmp_path, monkeypatch, fault):
    candidate = tmp_path / "candidate"
    copy_candidate(candidate)
    source = workspace(tmp_path / "baseline")
    with monkeypatch.context() as patch:
        patch.chdir(source)
        bundle = inject.build("AA" if fault == "F8" else fault, tmp_path / "bundles")
        if fault == "F8":
            # The study builds F8 from AA plus this lock diff; it is not a demo fault.
            recorded = json.loads((ROOT / "docs/results/f8-repair.json").read_text())["f8"]
            assert recorded["tag"] == "b12557"
            lock = bundle / "serving/llama_cpp.lock"
            lock.write_text(inject.set_yaml_value(lock.read_text(), "commit", recorded["commit"]))
    shutil.copytree(bundle / "serving", candidate / "serving", dirs_exist_ok=True)
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("PYTHONPATH", "BASH_ENV", "PYTEST_ADDOPTS", "DRIFTGATE_BUILD_STEPS")
    }
    # Reuse installed dependencies without changing the shared venv's editable installation.
    env["UV_NO_SYNC"] = "1"
    log = tmp_path / "suite.log"
    with log.open("w") as output:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--ignore=tests/test_fr16_candidate_suite.py"],
            cwd=candidate,
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
            timeout=900,
        )
    assert result.returncode == 0, log.read_text()
