"""Exercise the build host bootstrap with local stand-ins for EC2's external tools."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def executable(path, script):
    path.write_text("#!/bin/bash\nset -eu\n" + script)
    path.chmod(0o755)


@pytest.fixture
def host(tmp_path, monkeypatch):
    checkout = tmp_path / "driftgate"
    checkout.mkdir()
    for name in ("deploy", "gate", "serving"):
        shutil.copytree(ROOT / name, checkout / name)
    models = {
        "reference": "reference.gguf",
        "ci": "test.gguf",
        "F1": "f1.gguf",
        "F2": "f2.gguf",
        "sweep-q6-k": "sweep.gguf",
    }
    model_commit = "91cad51170dc346986eccefdc2dd33a9da36ead9"
    origin = tmp_path / "origin"
    published = origin / "Qwen/test-model" / "resolve" / model_commit
    published.mkdir(parents=True)
    for name in models.values():
        (published / name).write_text(name)
    (checkout / "serving/models.yaml").write_text(
        yaml.safe_dump(
            {
                "models_dir": "models",
                "url_template": f"{origin.as_uri()}/{{repo}}/resolve/{{commit}}/{{file}}",
                "models": [
                    {"role": role, "file": name, "repo": "Qwen/test-model", "commit": model_commit}
                    for role, name in models.items()
                ],
            }
        )
    )
    names = ("reference.gguf", "test.gguf", "f1.gguf", "f2.gguf")
    manifest = {
        "schema_version": 1,
        "artifacts": {
            name: {"sha256": hashlib.sha256(name.encode()).hexdigest()} for name in names
        },
    }
    (checkout / "serving/manifest.json").write_text(json.dumps(manifest))
    engine = tmp_path / "llama.cpp"
    (engine / ".git").mkdir(parents=True)
    bin_dir = engine / "build/bin"
    bin_dir.mkdir(parents=True)
    commands = tmp_path / "commands"
    commands.mkdir()
    log = tmp_path / "log"
    executable(
        commands / "uv",
        """
echo "uv $*" >> "$CALLS"
mkdir -p .venv/bin
printf '#!/bin/bash\nexec "$TEST_PYTHON" "$@"\n' > .venv/bin/python
chmod +x .venv/bin/python
""",
    )
    executable(commands / "git", 'echo "git $*" >> "$CALLS"\n')
    executable(
        commands / "cmake",
        """
echo "cmake $*" >> "$CALLS"
if [ "$1" = --build ]; then
  shift 5
  for target in "$@"; do
    printf '#!/bin/bash\necho "version: (commit 8c16babba)"\n' > "$LLAMA_CPP_BIN/$target"
    chmod +x "$LLAMA_CPP_BIN/$target"
  done
fi
""",
    )
    executable(commands / "aws", 'echo "unexpected AWS call" >&2\nexit 1\n')
    for key, value in {
        "PATH": f"{commands}:{os.environ['PATH']}",
        "LLAMA_CPP_BIN": bin_dir,
        "DRIFTGATE_LLAMA_CPP_DIR": engine,
        "TEST_PYTHON": sys.executable,
        "CALLS": log,
    }.items():
        monkeypatch.setenv(key, str(value))
    return checkout, log, names, published


def bootstrap(checkout):
    return subprocess.run(
        [str(checkout / "deploy/bootstrap_build_host.sh")],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_bootstrap_builds_both_locked_tools_and_reuses_verified_models(host):
    checkout, log, names, published = host
    result = bootstrap(checkout)
    assert result.returncode == 0, result.stderr
    assert (checkout / ".bootstrap-done").exists()
    calls = log.read_text()
    assert "uv sync --frozen" in calls
    assert "checkout --quiet --detach 8c16babba5d4c66e28a19d86e572e4b04f522d52" in calls
    assert "--target llama-server llama-perplexity" in calls
    for name in names:
        assert (checkout / "models" / name).read_text() == name
    assert not (checkout / "models/sweep.gguf").exists()

    shutil.rmtree(published)
    log.write_text("")
    result = bootstrap(checkout)
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == ["uv sync --frozen"]


def test_bootstrap_rejects_corrupt_download_before_marking_host_ready(host):
    checkout, _, names, published = host
    (published / names[0]).write_text("corrupt")
    result = bootstrap(checkout)
    assert result.returncode != 0
    report = json.loads(result.stdout)
    assert report["artifacts"][0]["status"] == "mismatch"
    assert not (checkout / ".bootstrap-done").exists()
