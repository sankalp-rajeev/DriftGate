"""D2: artifact KL divergence and same-top-token rate (FR-05, PRD Section 6.2.1). Build host.

`llama-perplexity -c 512` over the fixed corpus `gate/data/d2_corpus.txt`:

  base  once, on the reference model: `--kl-divergence-base <file>` saves the reference logits
        under the reference key (gate/reference.py) as `d2_base.kld`, next to the reference
        measurement; S3 for the build host; never git
  run   on the served model: `--kl-divergence-base <file> --kl-divergence` and parse mean KL,
        99th-percentile KL and same-top-token % from the tool's summary

Measurement only: no threshold here (A/A calibration, FR-12). Output is JSON (`driftgate.d2/1`).
Always `--threads 2` (gate config `d2.threads`, PRD Section 6.8), for the reference logits and
for every candidate, on every machine, whatever the serving config says.
Exit 0 on a completed measurement, 2 if the tool fails.

CLI: `python -m gate.d2 {base,run} [--config serving/config.yaml] [--instance-type T]
      [--reference-key KEY] [--out d2.json]`
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from gate import reference as ref_store
from gate.manifest import DEFAULT_CONFIG, load_yaml, sha256_file
from gate.serve import llama_bin_dir

SCHEMA = "driftgate.d2/1"
GATE_CONFIG = Path("gate/config.yaml")

_PATTERNS = {
    "mean_kld": r"Mean\s+KLD:\s+(-?[\d.]+)",
    "p99_kld": r"99\.0%\s+KLD:\s+(-?[\d.]+)",
    "max_kld": r"Maximum KLD:\s+(-?[\d.]+)",
    "median_kld": r"Median\s+KLD:\s+(-?[\d.]+)",
    "same_top_p_pct": r"Same top p:\s+(-?[\d.]+)",
}


def parse_output(text: str) -> dict:
    out = {}
    for key, pat in _PATTERNS.items():
        m = re.search(pat, text)
        if m is None:
            raise ValueError(f"llama-perplexity output has no {key}")
        out[key] = float(m.group(1))
    return out


def _cmd(serving: dict, d2: dict, model: Path, extra: list[str]) -> list[str]:
    return [
        str(llama_bin_dir() / "llama-perplexity"),
        "--model", str(model),
        "--ctx-size", str(d2["ctx_size"]),
        "--threads", str(d2["threads"]),
        *extra,
    ]  # fmt: skip


def _model(serving: dict) -> Path:
    return Path(load_yaml(Path(serving["models"]))["models_dir"]) / serving["model"]


def base(serving: dict, d2: dict, path: Path) -> dict:
    reference = next(
        m["file"] for m in load_yaml(Path(serving["models"]))["models"] if m["role"] == "reference"
    )
    if serving["model"] != reference:
        raise SystemExit(f"base logits must come from the reference model {reference}")
    path.parent.mkdir(parents=True, exist_ok=True)
    extra = ["--file", d2["corpus"], "--kl-divergence-base", str(path)]
    proc = subprocess.run(_cmd(serving, d2, _model(serving), extra), capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr[-4000:])
        raise SystemExit(2)
    return {
        "schema": SCHEMA,
        "action": "base",
        "model": serving["model"],
        "corpus_sha256": sha256_file(Path(d2["corpus"])),
        "base_logits": str(path),
        "threads": d2["threads"],
        "base_logits_sha256": sha256_file(path),
        "base_logits_bytes": path.stat().st_size,
    }


def run(serving: dict, d2: dict, path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"missing reference logits {path}; run `python -m gate.d2 base` first")
    extra = ["--kl-divergence-base", str(path), "--kl-divergence"]
    proc = subprocess.run(_cmd(serving, d2, _model(serving), extra), capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr[-4000:])
        raise SystemExit(2)
    return {
        "schema": SCHEMA,
        "detector": "D2",
        "action": "run",
        "model": serving["model"],
        "ctx_size": d2["ctx_size"],
        "threads": d2["threads"],
        "base_logits": str(path),
        "base_logits_sha256": sha256_file(path),
        "metrics": parse_output(proc.stdout + proc.stderr),
    }


def base_logits_path(
    gate_config: dict, serving: dict, instance: str | None, key: str | None
) -> Path:
    """Reference logits sit under the same key as the reference measurement (PRD 6.8)."""
    key = ref_store.resolve_key(gate_config, serving, instance, key)
    return ref_store.local_dir(gate_config, key) / ref_store.D2_BASE_FILE


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="gate.d2", description=__doc__.splitlines()[0])
    p.add_argument("action", choices=["base", "run"])
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--gate-config", type=Path, default=GATE_CONFIG)
    p.add_argument("--instance-type", help=f"default: ${ref_store.INSTANCE_ENV} or local")
    p.add_argument("--reference-key", help="default: gate config reference_store.key")
    p.add_argument("--out", type=Path)
    args = p.parse_args(argv)
    gate_config = load_yaml(args.gate_config)
    serving, d2 = load_yaml(args.config), gate_config["d2"]
    path = base_logits_path(gate_config, serving, args.instance_type, args.reference_key)
    result = base(serving, d2, path) if args.action == "base" else run(serving, d2, path)
    text = json.dumps(result, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
