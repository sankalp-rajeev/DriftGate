"""Gate CLI: measure D3, D5 and D6 against a live llama-server and emit JSON (FR-06, FR-08, FR-09).

  python -m gate measure --budget 16 --seed 0 --out runs/x/candidate.json
  python -m gate compare --candidate runs/x/candidate.json --out runs/x/verdict.json
  python -m gate run --budget 16 --seed 0 --out runs/x/verdict.json

  python -m gate reference --seed 0 [--upload]     # reference measurement, PRD Section 6.8
  python -m gate reference --print-key             # just the reference key

`measure` records raw per-item outputs; `compare` turns the reference measurement and a candidate
measurement into a verdict; `run` does both; `reference` measures the reference revision and
stores it under `reference/<model-sha256>/<config-hash>/<instance-type>/` (gate/reference.py).
Data paths, the reference store, the gate mode and the thresholds file come from
`gate/config.yaml`; the server address from the serving config.

Gate mode (PRD Section 6.8): `report` records the verdict and exits 0; `enforce` scores every
detector against the thresholds file (gate/score.py) and exits 1 when the gate score is above 1;
`auto` is `enforce` once the thresholds file exists (produced by A/A calibration, FR-12) and
`report` until then. `DRIFTGATE_FORCE_FAIL=1` makes `run` and `compare` exit 1 in any mode
(rollback drills). Exit 2 is a usage or input error, including a missing reference.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from contextlib import nullcontext
from pathlib import Path

from gate import d3, d5, d6, telemetry
from gate import reference as ref_store
from gate.llama_client import LlamaServer
from gate.manifest import load_yaml
from gate.score import THRESHOLDS_SCHEMA, score

MEASUREMENT_SCHEMA = "driftgate.measurement/1"
VERDICT_SCHEMA = "driftgate.verdict/2"
REFERENCE_SCHEMA = "driftgate.reference/1"
DEFAULT_GATE_CONFIG = Path("gate/config.yaml")
DEFAULT_SERVING_CONFIG = Path("serving/config.yaml")
FORCE_FAIL_ENV = "DRIFTGATE_FORCE_FAIL"
EXIT_PASS, EXIT_FAIL, EXIT_ERROR = 0, 1, 2
RUN_ID_ENV = "DRIFTGATE_RUN_ID"
__all__ = ["THRESHOLDS_SCHEMA", "main"]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _span(tel, detector: str):
    """FR-24 detector span, or a no-op when no telemetry is passed."""
    return nullcontext() if tel is None else tel.detector(detector)


def server_url(serving_config: dict) -> str:
    return f"http://{serving_config['server']['host']}:{serving_config['server']['port']}"


# ---------- measurement ----------


def measure(
    server: LlamaServer,
    budget: int,
    seed: int,
    prompts_path: Path,
    panel_path: Path,
    config_path: Path | None = None,
    tel=None,
    d5_budget: int | None = None,
    skip_d3: bool = False,
) -> dict:
    """`budget` D3 prompts and `d5_budget` D5 items (default: `budget`), both file prefixes.

    `skip_d3` leaves D3 empty (the RQ2 quantization sweep measures D5 only).
    """
    prompts = _jsonl(prompts_path)
    panel = _jsonl(panel_path)
    d5_budget = budget if d5_budget is None else d5_budget
    if not 1 <= budget <= len(prompts) or not 1 <= d5_budget <= len(panel):
        raise ValueError(
            f"budget {budget}/{d5_budget} outside 1..{len(prompts)} prompts, "
            f"1..{len(panel)} panel items"
        )
    props = server.props()
    t0 = time.monotonic()
    with _span(tel, "D3"):
        d3_run = [] if skip_d3 else d3.measure(server, prompts[:budget], seed)
    t1 = time.monotonic()
    with _span(tel, "D5"):
        d5_run = d5.measure(server, panel[:d5_budget], seed)
    t2 = time.monotonic()
    return {
        "schema": MEASUREMENT_SCHEMA,
        "budget": budget,
        "d5_budget": d5_budget,
        "seed": seed,
        "server": {
            "url": server.base_url,
            "model_path": props.get("model_path"),
            "build_info": props.get("build_info"),
        },
        "inputs": {
            "config": None if config_path is None else str(config_path),
            "config_sha256": None if config_path is None else _sha256(config_path),
            "d3_prompts": str(prompts_path),
            "d3_prompts_sha256": _sha256(prompts_path),
            "d5_panel": str(panel_path),
            "d5_panel_sha256": _sha256(panel_path),
        },
        "wall_clock_s": {"D3": t1 - t0, "D5": t2 - t1},
        "d3": d3_run,
        "d5": d5_run,
    }


# ---------- comparison ----------


def _check_pair(reference: dict, candidate: dict) -> None:
    for m in (reference, candidate):
        if m.get("schema") != MEASUREMENT_SCHEMA:
            raise ValueError(f"unsupported measurement schema {m.get('schema')!r}")
    if reference["seed"] != candidate["seed"]:
        raise ValueError("reference and candidate seeds differ")
    for key in ("d3_prompts_sha256", "d5_panel_sha256"):
        if reference["inputs"][key] != candidate["inputs"][key]:
            raise ValueError(f"reference and candidate use different data ({key})")
    for det in ("d3", "d5"):
        missing = {r["id"] for r in candidate[det]} - {r["id"] for r in reference[det]}
        if missing:
            raise ValueError(
                f"{det}: candidate items missing from reference: {sorted(missing)[:5]}"
            )


def _meta(m: dict) -> dict:
    return {k: m[k] for k in ("budget", "server", "inputs", "wall_clock_s")}


def detectors(reference: dict, candidate: dict, tel=None) -> dict:
    _check_pair(reference, candidate)
    out = {
        "D3": d3.metrics(reference["d3"], candidate["d3"]),
        "D5": d5.metrics(reference["d5"], candidate["d5"]),
    }
    with _span(tel, "D6"):  # D6 has no measurement of its own: latency from the D3 run
        out["D6"] = d6.metrics(reference["d3"], candidate["d3"])
    return out


# ---------- decision ----------


def resolve_mode(mode: str, thresholds_path: Path) -> str:
    if mode == "auto":
        return "enforce" if thresholds_path.exists() else "report"
    if mode not in ("report", "enforce"):
        raise ValueError(f"unknown gate mode {mode!r}")
    return mode


def decide(dets: dict, mode: str, thresholds_path: Path, env=None) -> dict:
    env = os.environ if env is None else env
    effective = resolve_mode(mode, thresholds_path)
    forced = env.get(FORCE_FAIL_ENV) == "1"
    scored = {"rules": None, "scores": None, "gate_score": None, "passed": None}
    if effective == "enforce":
        if not thresholds_path.exists():
            raise ValueError(f"enforce mode needs {thresholds_path}")
        scored = score(dets, json.loads(thresholds_path.read_text()))
    fail = forced or scored["passed"] is False
    return {
        "mode": effective,
        "configured_mode": mode,
        "thresholds_file": str(thresholds_path) if thresholds_path.exists() else None,
        "rules": scored["rules"],
        "scores": scored["scores"],
        "gate_score": scored["gate_score"],
        "passed": scored["passed"],
        "forced_fail": forced,
        "exit_code": EXIT_FAIL if fail else EXIT_PASS,
    }


def verdict(reference: dict, candidate: dict, gate_config: dict, env=None, tel=None) -> dict:
    t0 = time.monotonic()
    dets = detectors(reference, candidate, tel)
    decision = decide(dets, gate_config["mode"], Path(gate_config["thresholds"]), env)
    return {
        "schema": VERDICT_SCHEMA,
        "budget": candidate["budget"],
        "seed": candidate["seed"],
        "reference": _meta(reference),
        "candidate": _meta(candidate),
        "detectors": dets,
        "compare_wall_clock_s": time.monotonic() - t0,
        "decision": decision,
    }


# ---------- CLI ----------


def _write(obj: dict, out: Path | None) -> None:
    text = json.dumps(obj, indent=2) + "\n"
    if out is None:
        sys.stdout.write(text)
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gate", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("measure", "compare", "run", "reference"):
        s = sub.add_parser(name)
        s.add_argument("--gate-config", type=Path, default=DEFAULT_GATE_CONFIG)
        s.add_argument("--out", type=Path, help="write JSON here (default: stdout)")
        s.add_argument("--config", type=Path, default=DEFAULT_SERVING_CONFIG)
        if name in ("measure", "run", "reference"):
            s.add_argument("--server", help="base URL (default: host and port from --config)")
            s.add_argument("--seed", type=int, required=name != "reference")
        if name in ("measure", "run"):
            where = s.add_mutually_exclusive_group(required=True)
            where.add_argument("--budget", type=int, help="D3 prompts and D5 items, both this many")
            where.add_argument("--level", help="budget level L1, L2 or L3 (gate config `levels`)")
        if name in ("compare", "run", "reference"):
            s.add_argument(
                "--instance-type",
                help=f"default: derived (IMDSv2 or local-host); ${ref_store.INSTANCE_ENV} wins",
            )
            s.add_argument(
                "--reference-key",
                help="default: the baseline key (gate/baseline.yaml); test override",
            )
        if name in ("compare", "run"):
            s.add_argument("--reference", type=Path, help="reference file; overrides the store")
        if name == "reference":
            s.add_argument("--budget", type=int, help="default: gate config reference_store")
            s.add_argument("--print-key", action="store_true", help="print the key and exit")
            s.add_argument("--upload", action="store_true", help="also copy to S3 (off by default)")
            s.add_argument("--s3-uri", help="default: gate config reference_store.s3_uri")
        if name == "compare":
            s.add_argument("--candidate", type=Path, required=True)
        if name == "run":
            s.add_argument("--measurement-out", type=Path, help="also save the raw measurement")
    return p


def main(argv: list[str] | None = None, tel=None) -> int:
    args = _parser().parse_args(argv)
    tel = tel or telemetry.from_env()
    run_id = os.environ.get(RUN_ID_ENV, "local")
    try:
        with tel.run(
            run_id, name=f"gate.cli.{args.cmd}", **{"driftgate.gate.cmd": args.cmd}
        ) as span:
            code = _main(args, tel, span, run_id)
    finally:
        tel.shutdown()
    return code


def _main(args, tel, span, run_id: str) -> int:
    try:
        gate_config = load_yaml(args.gate_config)
        if args.cmd == "reference":
            return _reference(args, gate_config, tel)
        if args.cmd in ("measure", "run"):
            url = args.server or server_url(load_yaml(args.config))
            if args.level:
                lv = gate_config["levels"][args.level]
                n3, n5 = lv["d3"], lv["d5"]
            else:
                n3, n5 = args.budget, args.budget
            m = measure(
                LlamaServer(url),
                n3,
                args.seed,
                Path(gate_config["data"]["d3_prompts"]),
                Path(gate_config["data"]["d5_panel"]),
                args.config,
                tel,
                d5_budget=n5,
            )
            if m["server"]["model_path"]:
                span.set_attribute("gen_ai.request.model", Path(m["server"]["model_path"]).name)
            if args.cmd == "measure":
                _write(m, args.out)
                return EXIT_PASS
            if args.measurement_out:
                _write(m, args.measurement_out)
            candidate = m
        else:
            candidate = json.loads(args.candidate.read_text())
        ref_path = reference_path(args, gate_config)
        v = verdict(json.loads(ref_path.read_text()), candidate, gate_config, tel=tel)
    except (OSError, ValueError, KeyError) as e:
        sys.stderr.write(f"gate: {type(e).__name__}: {e}\n")
        span.set_attribute("process.exit.code", EXIT_ERROR)
        return EXIT_ERROR
    tel.record_verdict(v, run_id)
    span.set_attribute("process.exit.code", v["decision"]["exit_code"])
    _write(v, args.out)
    return v["decision"]["exit_code"]


# ---------- reference ----------


def reference_path(args, gate_config: dict) -> Path:
    """--reference, else gate config `reference` (a file), else the keyed reference store."""
    if args.reference:
        return args.reference
    if gate_config.get("reference"):
        return Path(gate_config["reference"])
    key = ref_store.resolve_key(
        gate_config, load_yaml(args.config), args.instance_type, args.reference_key
    )
    path = ref_store.local_dir(gate_config, key) / ref_store.MEASUREMENT_FILE
    if not path.exists():
        raise ValueError(f"no reference measurement at {path}; run `python -m gate reference`")
    return path


def _reference(args, gate_config: dict, tel) -> int:
    serving = load_yaml(args.config)
    key = ref_store.resolve_key(gate_config, serving, args.instance_type, args.reference_key)
    local = ref_store.local_dir(gate_config, key)
    uri = ref_store.s3_uri(gate_config, key, args.s3_uri)
    if args.print_key:
        _write({"key": key, "local_dir": str(local), "s3_uri": uri}, args.out)
        return EXIT_PASS
    if args.seed is None:
        raise ValueError("--seed is required to measure a reference")
    if not args.reference_key and not ref_store.is_baseline(gate_config, serving):
        raise ValueError(
            "serving config is not the baseline in gate/baseline.yaml; "
            "a reference is measured only on the baseline release"
        )
    if args.upload and uri is None:
        raise ValueError("--upload needs reference_store.s3_uri or --s3-uri")
    budget = args.budget or gate_config["reference_store"]["budget"]
    url = args.server or server_url(serving)
    m = measure(
        LlamaServer(url),
        budget,
        args.seed,
        Path(gate_config["data"]["d3_prompts"]),
        Path(gate_config["data"]["d5_panel"]),
        args.config,
        tel,
    )
    m["reference"] = {
        "schema": REFERENCE_SCHEMA,
        "key": key,
        "model_sha256": key.split("/")[1],
        "config_hash": key.split("/")[2],
        "instance_type": key.split("/")[3],
    }
    path = local / ref_store.MEASUREMENT_FILE
    _write(m, path)
    uploaded = None
    if args.upload:
        uploaded = uri + ref_store.MEASUREMENT_FILE
        ref_store.upload(path, uploaded)
    _write({"key": key, "path": str(path), "uploaded": uploaded}, args.out)
    return EXIT_PASS
