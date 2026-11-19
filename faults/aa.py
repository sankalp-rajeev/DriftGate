"""A/A harness (FR-10, feeds FR-12): repeat an identical redeploy and record detector outputs.

Each run starts llama-server from a bundle (default: the AA bundle from `faults.inject AA`),
waits for `/health`, measures D3, D5 and D6 with an explicit `--seed`, compares against the
stored reference measurement, writes `run-NN.json` (the verdict) and `run-NN.measurement.json`,
then stops the server. The same harness runs a faulted bundle (`--bundle artifacts/bundles/F4`).

  python -m faults.aa --runs 20 --level L3 --seed 0 --d2 --d1 \\
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

from gate import cli, d1, d2
from gate import reference as ref_store
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


def reference_measurement(args, gate_config: dict) -> dict:
    """--reference, else the baseline's reference measurement in the local store."""
    if args.reference:
        return json.loads(args.reference.read_text())
    key = ref_store.resolve_key(gate_config, {}, args.instance_type)
    path = ref_store.local_dir(gate_config, key) / ref_store.MEASUREMENT_FILE
    return json.loads(path.read_text())


def levels(args, gate_config: dict) -> tuple[int, int]:
    """(D3 prompts, D5 items) for --level, else --budget for both."""
    if args.level:
        lv = gate_config["levels"][args.level]
        return lv["d3"], lv["d5"]
    return args.budget, args.budget


def run(args) -> list[Path]:
    gate_config = load_yaml(args.gate_config)
    reference = reference_measurement(args, gate_config)
    prompts = Path(gate_config["data"]["d3_prompts"])
    panel = Path(gate_config["data"]["d5_panel"])
    bundle_meta = json.loads((args.bundle / "bundle.json").read_text())
    bundle_config = load_yaml(args.bundle / "serving/config.yaml")
    n3, n5 = levels(args, gate_config)
    args.out.mkdir(parents=True, exist_ok=True)
    written = []
    for i in range(args.start, args.start + args.runs):
        t0 = time.monotonic()
        proc, base = start_server(args.bundle, args.models_dir)
        startup_s = time.monotonic() - t0
        try:
            m = cli.measure(
                LlamaServer(base),
                n3,
                args.seed,
                prompts_path=prompts,
                panel_path=panel,
                config_path=args.bundle / "serving/config.yaml",
                d5_budget=n5,
                skip_d3=args.d5_only,
            )
        finally:
            stop_server(proc)
        (args.out / f"run-{i:02d}.measurement.json").write_text(json.dumps(m) + "\n")
        seconds = {"startup": startup_s, **m.get("wall_clock_s", {})}
        if args.d2:  # llama-perplexity against the baseline's stored logits (server is stopped)
            key = ref_store.resolve_key(gate_config, {}, args.instance_type)
            base_path = ref_store.local_dir(gate_config, key) / ref_store.D2_BASE_FILE
            t = time.monotonic()
            result = d2.run(bundle_config, gate_config["d2"], base_path)
            seconds["D2"] = time.monotonic() - t
            (args.out / f"run-{i:02d}.d2.json").write_text(json.dumps(result, indent=2) + "\n")
        if args.d1:
            template = args.bundle / bundle_config["chat_template"]
            t = time.monotonic()
            result = d1.run(args.bundle / "serving/config.yaml", template, args.out / f"d1-{i:02d}")
            seconds["D1"] = time.monotonic() - t
            (args.out / f"run-{i:02d}.d1.json").write_text(json.dumps(result, indent=2) + "\n")
        verdict = None if args.d5_only else cli.verdict(reference, m, gate_config)
        record = {
            "schema": AA_SCHEMA,
            "run": i,
            "seed": args.seed,
            "run_seed": args.run_seed if args.run_seed is not None else i,
            "level": args.level,
            "budget": n3,
            "d5_budget": n5,
            "bundle": {"path": str(args.bundle), **bundle_meta},
            "startup_s": startup_s,
            "seconds": seconds,
            "verdict": verdict,
        }
        path = args.out / f"run-{i:02d}.json"
        path.write_text(json.dumps(record, indent=2) + "\n")
        written.append(path)
        print(
            json.dumps({"run": i, "level": args.level, "bundle": bundle_meta["fault"]}), flush=True
        )
    return written


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="faults.aa", description=__doc__.splitlines()[0])
    p.add_argument("--runs", type=int, required=True)
    p.add_argument("--start", type=int, default=0, help="index of the first run (to resume)")
    where = p.add_mutually_exclusive_group(required=True)
    where.add_argument("--budget", type=int, help="D3 prompts and D5 items, both this many")
    where.add_argument("--level", help="budget level L1, L2 or L3 (gate config `levels`)")
    p.add_argument(
        "--seed", type=int, required=True, help="measurement seed (must match the reference)"
    )
    p.add_argument(
        "--run-seed", type=int, help="per-run seed (hidden-pool sampling); default: run index"
    )
    p.add_argument("--d2", action="store_true", help="also run D2 (A/A, F1, F2, F3, F8, sweep)")
    p.add_argument("--d1", action="store_true", help="also run D1 on the bundle")
    p.add_argument("--d5-only", action="store_true", help="measure D5 only (quantization sweep)")
    p.add_argument("--instance-type", help="default: derived (IMDSv2 or local-host)")
    p.add_argument("--gate-config", type=Path, default=cli.DEFAULT_GATE_CONFIG)
    p.add_argument("--reference", type=Path, help="default: the baseline reference measurement")
    p.add_argument("--bundle", type=Path, default=Path("artifacts/bundles/AA"))
    p.add_argument("--models-dir", type=Path, default=Path("models"))
    p.add_argument("--out", type=Path, required=True)
    run(p.parse_args(argv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
