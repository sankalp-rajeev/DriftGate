"""FR-05 (D2): mean KL, 99th-percentile KL and same-top-token % from llama-perplexity."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

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
    assert cfg["base_logits"].startswith("artifacts/reference/")


BASE = Path(load_yaml(Path("gate/config.yaml"))["d2"]["base_logits"])
BIN = d2.llama_bin_dir() / "llama-perplexity"


@pytest.mark.skipif(
    not (BASE.exists() and os.access(BIN, os.X_OK)), reason="needs reference logits and llama.cpp"
)
def test_reference_against_itself_is_zero_kl(tmp_path):
    out = tmp_path / "d2.json"
    subprocess.run(
        [sys.executable, "-m", "gate.d2", "run", "--out", str(out)], check=True, timeout=900
    )
    m = json.loads(out.read_text())["metrics"]
    assert m["mean_kld"] < 1e-3
    assert m["same_top_p_pct"] > 99.0
