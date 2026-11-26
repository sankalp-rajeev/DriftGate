"""FR-01: llama-server answers /health and exposes Prometheus text on /metrics.

Needs the built llama-server and the CI model (`make models`); skipped otherwise, so CI without
a model stays green. The systemd and instance-restart parts are verified in the Lab (T-12).
Local stand-in for "after restart": stop the server and start it again from the same config.
"""

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
from baseline_serving import MANIFEST, serving_config
from baseline_serving import MODELS as MODELS_PATH

from gate import manifest, serve

CONFIG = serving_config()
MODELS = manifest.load_yaml(MODELS_PATH)
CI_MODEL = next(m["file"] for m in MODELS["models"] if m["role"] == "ci")
LLAMA_SERVER = serve.llama_bin_dir() / "llama-server"

pytestmark = pytest.mark.skipif(
    not os.access(LLAMA_SERVER, os.X_OK)
    or not manifest.verify([CI_MODEL], MANIFEST, models_dir=Path(MODELS["models_dir"]))["ok"],
    reason="needs llama-server and the CI model matching the manifest",
)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return r.status, r.read().decode()
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        return None, ""


def _wait_health(base, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, body = _get(base + "/health")
        if status == 200:
            return body
        time.sleep(0.5)
    raise AssertionError("llama-server /health not ok in time")


@pytest.fixture
def ci_config(tmp_path):
    cfg = dict(CONFIG, model=CI_MODEL, threads=4)
    cfg["server"] = dict(CONFIG["server"], port=_free_port())
    cfg["context"] = {"ctx_size": 2048}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return path, cfg


def _start(path):
    return subprocess.Popen(
        [sys.executable, "-m", "gate.serve", "--config", str(path), "--manifest", str(MANIFEST)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_health_and_metrics_survive_restart(ci_config):
    path, cfg = ci_config
    base = f"http://{cfg['server']['host']}:{cfg['server']['port']}"
    for _ in range(2):  # boot, then restart
        proc = _start(path)
        try:
            assert '"ok"' in _wait_health(base)
            status, text = _get(base + "/metrics")
            assert status == 200
            assert "# TYPE llamacpp:" in text
        finally:
            proc.terminate()
            proc.wait(timeout=30)


def test_serve_refuses_model_that_differs_from_manifest(tmp_path, ci_config):
    path, _ = ci_config
    bad = tmp_path / "manifest.json"
    bad.write_text('{"schema_version": 1, "artifacts": {}}')
    out = subprocess.run(
        [sys.executable, "-m", "gate.serve", "--config", str(path), "--manifest", str(bad)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 1
    assert "does not match the manifest" in out.stderr
