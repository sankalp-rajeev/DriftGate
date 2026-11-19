"""A/A harness (FR-10, feeds FR-12): repeat an identical redeploy and record detector outputs.

Each run starts llama-server from a bundle (default: the AA bundle from `faults.inject AA`),
waits for `/health`, measures D3, D5 and D6 with an explicit `--seed`, compares against the
stored reference measurement, writes `run-NN.json` (the verdict) and `run-NN.measurement.json`,
then stops the server. The same harness runs a faulted bundle (`--bundle artifacts/bundles/F4`).

  python -m faults.aa --runs 20 --budget 16 --seed 0 \\
      --out runs/2026-11-21-aa
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from gate import cli
from gate.llama_client import LlamaServer
from gate.manifest import load_yaml

AA_SCHEMA = "driftgate.aa-run/1"


def _healthy(base: str) -> bool:
    try:
        with urllib.request.urlopen(base + "/health", timeout=2) as r:
            return r.status == 200
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        return False


def start_server(bundle: Path, models_dir: Path, timeout: float = 180) -> tuple:
    config = load_yaml(bundle / "serving/config.yaml")
    base = cli.server_url(config)
    if _healthy(base):
        raise SystemExit(f"something is already serving on {base}; stop it first")
    log = open(bundle / "server.log", "ab")
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "gate.serve",
            "--config", "serving/config.yaml",
            "--manifest", "serving/manifest.json",
            "--models-dir", str(models_dir.resolve()),
        ],
        cwd=bundle, stdout=log, stderr=subprocess.STDOUT,
    )  # fmt: skip
    deadline = time.monotonic() + timeout
    while not _healthy(base):
        if proc.poll() is not None or time.monotonic() > deadline:
            proc.terminate()
            raise SystemExit(f"llama-server from {bundle} did not become healthy; see server.log")
        time.sleep(0.5)
    return proc, base


def stop_server(proc) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def run(args) -> list[Path]:
    gate_config = load_yaml(args.gate_config)
    reference = json.loads((args.reference or Path(gate_config["reference"])).read_text())
    prompts = Path(gate_config["data"]["d3_prompts"])
    panel = Path(gate_config["data"]["d5_panel"])
    bundle_meta = json.loads((args.bundle / "bundle.json").read_text())
    args.out.mkdir(parents=True, exist_ok=True)
    written = []
    for i in range(args.start, args.start + args.runs):
        t0 = time.monotonic()
        proc, base = start_server(args.bundle, args.models_dir)
        startup_s = time.monotonic() - t0
        try:
            m = cli.measure(
                LlamaServer(base),
                args.budget,
                args.seed,
                prompts_path=prompts,
                panel_path=panel,
                config_path=args.bundle / "serving/config.yaml",
            )
        finally:
            stop_server(proc)
        verdict = cli.verdict(reference, m, gate_config)
        record = {
            "schema": AA_SCHEMA,
            "run": i,
            "seed": args.seed,
            "budget": args.budget,
            "bundle": {"path": str(args.bundle), **bundle_meta},
            "startup_s": startup_s,
            "verdict": verdict,
        }
        (args.out / f"run-{i:02d}.measurement.json").write_text(json.dumps(m) + "\n")
        path = args.out / f"run-{i:02d}.json"
        path.write_text(json.dumps(record, indent=2) + "\n")
        written.append(path)
        d = verdict["detectors"]
        print(
            json.dumps(
                {
                    "run": i,
                    "d3_mean_agreement_share": d["D3"]["mean_agreement_share"],
                    "d3_mean_approx_kl": d["D3"]["mean_approx_kl"],
                    "d5_delta_accuracy": d["D5"]["delta_accuracy"],
                    "d6_p95_ttft_ms": d["D6"]["candidate"]["p95_ttft_ms"],
                }
            ),
            flush=True,
        )
    return written


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="faults.aa", description=__doc__.splitlines()[0])
    p.add_argument("--runs", type=int, required=True)
    p.add_argument("--start", type=int, default=0, help="index of the first run (to resume)")
    p.add_argument("--budget", type=int, required=True)
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--gate-config", type=Path, default=cli.DEFAULT_GATE_CONFIG)
    p.add_argument("--reference", type=Path, help="default: gate config `reference`")
    p.add_argument("--bundle", type=Path, default=Path("artifacts/bundles/AA"))
    p.add_argument("--models-dir", type=Path, default=Path("models"))
    p.add_argument("--out", type=Path, required=True)
    run(p.parse_args(argv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
