"""FR-05 (D2): mean KL, 99th-percentile KL and same-top-token % from llama-perplexity."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from baseline_serving import serving_config

from gate import d2
from gate.manifest import load_yaml

SAMPLE = """
====== Perplexity statistics ======
Mean PPL(Q)                   :   9.811562 ±   0.522131
====== KL divergence statistics ======
Mean    KLD:   0.012345 ±   0.000611
Maximum KLD:   1.234567
99.9%   KLD:   0.900000
99.0%   KLD:   0.150000
95.0%   KLD:   0.050000
Median  KLD:   0.004000
====== Token probability statistics ======
Same top p: 96.875 ±  0.544 %
"""


def test_parse_llama_perplexity_summary():
    assert d2.parse_output(SAMPLE) == {
        "mean_kld": 0.012345,
        "p99_kld": 0.15,
        "max_kld": 1.234567,
        "median_kld": 0.004,
        "same_top_p_pct": 96.875,
    }


def test_parse_fails_loudly_on_missing_fields():
    with pytest.raises(ValueError, match="same_top_p_pct"):
        d2.parse_output(SAMPLE.split("Same top p")[0])


def test_gate_config_uses_prd_corpus_and_context():
    cfg = load_yaml(Path("gate/config.yaml"))["d2"]
    assert cfg["corpus"] == "gate/data/d2_corpus.txt"
    assert cfg["ctx_size"] == 512
    assert cfg["threads"] == 2  # PRD Section 6.8: always 2, on every machine


def test_d2_always_uses_two_threads_whatever_the_serving_config_says():
    d2cfg = load_yaml(Path("gate/config.yaml"))["d2"]
    for threads in (-1, 4, 32):
        cmd = d2._cmd({"threads": threads}, d2cfg, Path("m.gguf"), [])
        assert cmd[cmd.index("--threads") + 1] == "2"


def test_reference_logits_live_under_the_reference_key():
    gate_cfg = load_yaml(Path("gate/config.yaml"))
    serving = serving_config()
    path = d2.base_logits_path(gate_cfg, serving, "t3.large", None)
    key = d2.ref_store.resolve_key(gate_cfg, serving, "t3.large")
    assert path == Path(gate_cfg["reference_store"]["local_root"]) / key / "d2_base.kld"
    pinned = d2.base_logits_path(gate_cfg, serving, None, "reference/a/b/c")
    assert pinned == Path(gate_cfg["reference_store"]["local_root"]) / "reference/a/b/c/d2_base.kld"


@pytest.mark.parametrize(("mean_kld", "exit_code"), [(0.0, 0), (0.000001, 1)])
def test_d2_cli_enforces_lab_threshold_before_deploy(
    tmp_path, baseline_config, mean_kld, exit_code
):
    """Stub only llama-perplexity; use the real CLI, parser and installed rules."""
    cfg = load_yaml(Path("gate/config.yaml"))
    cfg["thresholds"] = str(Path("gate/thresholds.json").resolve())
    cfg["reference_store"]["local_root"] = str(tmp_path / "artifacts")
    gate_config = tmp_path / "gate.json"
    gate_config.write_text(json.dumps(cfg))
    key = "reference/test/config/t3.large"
    logits = Path(cfg["reference_store"]["local_root"]) / key / "d2_base.kld"
    logits.parent.mkdir(parents=True)
    logits.write_text("reference logits fixture")
    binary = tmp_path / "bin/llama-perplexity"
    binary.parent.mkdir()
    binary.write_text(
        "#!/bin/sh\ncat <<'OUTPUT'\n" + SAMPLE.replace("0.012345", f"{mean_kld:.6f}") + "\nOUTPUT\n"
    )
    binary.chmod(0o755)
    env = dict(os.environ, LLAMA_CPP_BIN=str(binary.parent))
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "gate.d2",
            "run",
            "--config",
            str(baseline_config),
            "--gate-config",
            str(gate_config),
            "--reference-key",
            key,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == exit_code, result.stderr
    report = json.loads(result.stdout)
    assert report["metrics"]["mean_kld"] == mean_kld
    assert report["decision"]["mode"] == "enforce"
    assert report["decision"]["passed"] is (exit_code == 0)


BASE = d2.base_logits_path(load_yaml(Path("gate/config.yaml")), serving_config(), None, None)
BIN = d2.llama_bin_dir() / "llama-perplexity"


@pytest.mark.skipif(
    not (BASE.exists() and os.access(BIN, os.X_OK)), reason="needs reference logits and llama.cpp"
)
def test_reference_against_itself_is_zero_kl(tmp_path, baseline_config):
    out = tmp_path / "d2.json"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "gate.d2",
            "run",
            "--config",
            str(baseline_config),
            "--out",
            str(out),
        ],
        check=True,
        timeout=900,
    )
    m = json.loads(out.read_text())["metrics"]
    assert m["mean_kld"] < 1e-3
    assert m["same_top_p_pct"] > 99.0
