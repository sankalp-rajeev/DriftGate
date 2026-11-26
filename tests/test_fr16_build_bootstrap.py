"""Exercise the build host bootstrap with local stand-ins for EC2's external tools."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import yaml
from baseline_serving import copy_serving

ROOT = Path(__file__).resolve().parents[1]
CACHE_URI = (
    "s3://lab-cache/bin/llama.cpp/8c16babba5d4c66e28a19d86e572e4b04f522d52/"
    "al2023-x86_64-avx512.tar.gz"
)


def executable(path, script):
    path.write_text("#!/bin/bash\nset -eu\n" + script)
    path.chmod(0o755)


@pytest.fixture
def host(tmp_path, monkeypatch):
    checkout = tmp_path / "driftgate"
    checkout.mkdir()
    for name in ("deploy", "gate", "observability"):
        shutil.copytree(ROOT / name, checkout / name)
    copy_serving(checkout)
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
  while [ "$1" != --target ]; do shift; done
  shift
  mkdir -p "$LLAMA_CPP_BIN"
  printf 'shared library' > "$LLAMA_CPP_BIN/libllama.so.1"
  ln -sf libllama.so.1 "$LLAMA_CPP_BIN/libllama.so"
  for target in "$@"; do
    printf '#!/bin/bash\necho "version: (commit 8c16babba)"\n' > "$LLAMA_CPP_BIN/$target"
    chmod +x "$LLAMA_CPP_BIN/$target"
  done
fi
""",
    )
    executable(commands / "aws", 'echo "unexpected AWS call" >&2\nexit 1\n')
    monkeypatch.delenv("DRIFTGATE_BUCKET", raising=False)
    monkeypatch.delenv("DRIFTGATE_BUNDLE_BUCKET", raising=False)
    for key, value in {
        "PATH": f"{commands}:{os.environ['PATH']}",
        "LLAMA_CPP_BIN": bin_dir,
        "DRIFTGATE_LLAMA_CPP_DIR": engine,
        "DRIFTGATE_BUILD_ENV": tmp_path / "build.env",
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
    assert "DriftGate build host READY" in result.stdout
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


@pytest.mark.parametrize(
    ("entrypoint", "cache_state"),
    [
        ("build", "hit"),
        ("build", "miss"),
        ("build", "version-mismatch"),
        ("serving", "hit"),
        ("serving", "miss"),
    ],
)
def test_instances_use_or_populate_the_binary_cache(
    host, tmp_path, monkeypatch, entrypoint, cache_state
):
    checkout, log, _, _ = host
    cache_object = tmp_path / "cached.tar.gz"
    if cache_state != "miss":
        cached_bin = tmp_path / "cached-bin"
        cached_bin.mkdir()
        (cached_bin / "libllama.so.1").write_text("cached shared library")
        (cached_bin / "libllama.so").symlink_to("libllama.so.1")
        executable(
            cached_bin / "llama-server",
            'test -f "$(dirname "$0")/libllama.so"\necho "version: (commit 8c16babba)"\n',
        )
        perplexity = "8c16babba" if cache_state == "hit" else "000000000"
        executable(cached_bin / "llama-perplexity", f'echo "version: (commit {perplexity})"\n')
        with tarfile.open(cache_object, "w:gz") as archive:
            archive.add(cached_bin, arcname="build/bin")

    commands = tmp_path / "commands"
    executable(
        commands / "aws",
        f"""
echo "aws $*" >> "$CALLS"
src="${{@: -2:1}}"
dest="${{@: -1}}"
if [[ "$src" = "{CACHE_URI}" ]]; then
  cp "{cache_object}" "$dest"
elif [[ "$dest" = "{CACHE_URI}" && " $* " = *" --sse AES256 "* ]]; then
  cp "$src" "{cache_object}"
elif [[ "$src" = s3://lab-cache/reference/* ]]; then
  mkdir -p "$dest"
  touch "$dest/measurement.json" "$dest/d2_base.kld"
else
  exit 1
fi
""",
    )
    if entrypoint == "build":
        (tmp_path / "build.env").write_text("DRIFTGATE_BUNDLE_BUCKET=lab-cache\n")
        result = bootstrap(checkout)
    else:
        settings = tmp_path / "deploy.env"
        settings.write_text("DRIFTGATE_BUCKET=lab-cache\n")
        for key, value in {
            "DRIFTGATE_INSTANCE_ENV": settings,
            "DRIFTGATE_APP_DIR": checkout,
            "DRIFTGATE_INSTANCE_SERVING_CONFIG": tmp_path / "serving.yaml",
            "DRIFTGATE_INSTANCE_TYPE": "t3.large",
            "DRIFTGATE_SYSTEMD": "0",
        }.items():
            monkeypatch.setenv(key, str(value))
        config = checkout / "serving/config.yaml"
        config.write_text(
            yaml.safe_dump(yaml.safe_load(config.read_text()) | {"model": "reference.gguf"})
        )
        result = subprocess.run(
            [str(checkout / "deploy/hooks/after_install.sh")],
            capture_output=True,
            text=True,
            timeout=30,
        )
    assert result.returncode == 0, result.stderr
    calls = log.read_text()
    assert f"aws s3 cp --only-show-errors {CACHE_URI}" in calls
    assert "aws s3 cp --only-show-errors --recursive s3://lab-cache/reference/" in calls
    refs = list((checkout / "artifacts/reference").glob("*/*/*"))
    assert len(refs) == 1
    assert (refs[0] / "measurement.json").is_file()
    assert (refs[0] / "d2_base.kld").is_file()
    bin_dir = tmp_path / "llama.cpp/build/bin"
    assert (bin_dir / "libllama.so").is_symlink()
    if cache_state == "hit":
        assert "cmake" not in calls and "git " not in calls
        assert (bin_dir / "libllama.so").read_text() == "cached shared library"
    else:
        assert "--target llama-server llama-perplexity" in calls
        assert "-DGGML_NATIVE=OFF" in calls
        assert "-DGGML_AVX512=ON" in calls
        for extension in ("VBMI", "VNNI", "BF16"):
            assert f"-DGGML_AVX512_{extension}=OFF" in calls
        assert "--sse AES256" in calls
        with tarfile.open(cache_object) as archive:
            assert set(archive.getnames()) >= {
                "build/bin/llama-server",
                "build/bin/llama-perplexity",
                "build/bin/libllama.so",
                "build/bin/libllama.so.1",
            }


@pytest.mark.parametrize("download", ["complete", "incomplete", "error"])
def test_build_restores_reference_before_d2_and_stops_on_restore_failure(
    host, tmp_path, monkeypatch, download
):
    checkout, log, _, _ = host
    assert bootstrap(checkout).returncode == 0
    commands = tmp_path / "commands"
    executable(commands / "git", "echo abcdef0123456789\n")
    executable(
        commands / "aws",
        f'''
echo "aws $*" >> "$CALLS"
if [[ "{download}" = error ]]; then exit 1; fi
dest="${{@: -1}}"
touch "$dest/measurement.json"
if [[ "{download}" = complete ]]; then touch "$dest/d2_base.kld"; fi
''',
    )
    executable(
        commands / "uv",
        """
ref="$(.venv/bin/python -m gate reference --print-key | .venv/bin/python -c \
  'import json,sys; print(json.load(sys.stdin)["local_dir"])')"
test -f "$ref/measurement.json" && test -f "$ref/d2_base.kld"
echo "D2 ran with reference" >> "$CALLS"
echo '{}'
""",
    )
    monkeypatch.setenv("DRIFTGATE_BUNDLE_BUCKET", "lab-cache")
    monkeypatch.setenv("DRIFTGATE_BUILD_STEPS", "d2")
    log.write_text("")
    result = subprocess.run(
        [
            str(checkout / "deploy/build.sh"),
            "--commit",
            "abcdef0123456789",
            "--run-id",
            "restore",
            "--out",
            str(tmp_path / "out"),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    report = json.loads((tmp_path / "out/build.json").read_text())
    calls = log.read_text()
    if download == "complete":
        assert result.returncode == 0, result.stderr
        assert report["status"] == "passed"
        assert calls.index("s3 cp") < calls.index("D2 ran with reference")
        # A second build reuses both local files.
        log.write_text("")
        second = subprocess.run(result.args, capture_output=True, text=True, timeout=30)
        assert second.returncode == 0, second.stderr
        assert log.read_text().splitlines() == ["D2 ran with reference"]
    else:
        assert result.returncode != 0
        assert report["failed_step"] == "reference"
        assert "D2 ran" not in calls
