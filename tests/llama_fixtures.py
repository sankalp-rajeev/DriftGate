"""llama-server fixtures for live tests: `ci_server` runs the CI model, or skips."""

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

from gate import manifest, serve

DEV_PORT = 8083  # local dev servers; 8080 is the reference server (PRD runs)
DEV_THREADS = 4  # keep test servers from starving a long reference run on the same CPU


def _free_port():
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", DEV_PORT))
        except OSError:
            s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _healthy(base):
    try:
        with urllib.request.urlopen(base + "/health", timeout=2) as r:
            return r.status == 200
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        return False


def ci_model_available() -> bool:
    models = manifest.load_yaml(Path("serving/models.yaml"))
    ci = next(m["file"] for m in models["models"] if m["role"] == "ci")
    binary = serve.llama_bin_dir() / "llama-server"
    if not os.access(binary, os.X_OK):
        return False
    return manifest.verify([ci], models_dir=Path(models["models_dir"]))["ok"]


@pytest.fixture(scope="module")
def ci_server(tmp_path_factory):
    """Base URL of a llama-server running the CI model from the serving config."""
    if not ci_model_available():
        pytest.skip("needs llama-server and the CI model matching the manifest")
    config = manifest.load_yaml(Path("serving/config.yaml"))
    models = manifest.load_yaml(Path("serving/models.yaml"))
    ci = next(m["file"] for m in models["models"] if m["role"] == "ci")
    cfg = dict(config, model=ci, threads=DEV_THREADS)
    cfg["server"] = dict(config["server"], port=_free_port())
    path = tmp_path_factory.mktemp("ci") / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    proc = subprocess.Popen(
        [sys.executable, "-m", "gate.serve", "--config", str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://{cfg['server']['host']}:{cfg['server']['port']}"
    try:
        deadline = time.monotonic() + 120
        while not _healthy(base):
            if time.monotonic() > deadline or proc.poll() is not None:
                pytest.fail("CI llama-server did not become healthy")
            time.sleep(0.5)
        yield base
    finally:
        proc.terminate()
        proc.wait(timeout=30)
