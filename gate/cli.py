"""Gate CLI: measure D3, D5 and D6 against a live llama-server and emit JSON (FR-06, FR-08, FR-09).

  python -m gate measure --budget 16 --seed 0 --out runs/x/candidate.json
  python -m gate compare --candidate runs/x/candidate.json --out runs/x/verdict.json
  python -m gate run --budget 16 --seed 0 --out runs/x/verdict.json

`measure` records raw per-item outputs; `compare` turns the reference measurement and a candidate
measurement into a verdict; `run` does both. Data paths, the reference measurement, the gate mode
and the thresholds file come from `gate/config.yaml`; the server address from the serving config.

Gate mode (PRD Section 6.8): `report` records the verdict and exits 0; `enforce` exits 1 when any
rule in the thresholds file fails; `auto` is `enforce` once the thresholds file exists (produced
by A/A calibration, FR-12) and `report` until then. `DRIFTGATE_FORCE_FAIL=1` makes `run` and
`compare` exit 1 in any mode (rollback drills). Exit 2 is a usage or input error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import operator
import os
import sys
import time
from pathlib import Path

from gate import d3, d5, d6
from gate.llama_client import LlamaServer
from gate.manifest import load_yaml

MEASUREMENT_SCHEMA = "driftgate.measurement/1"
VERDICT_SCHEMA = "driftgate.verdict/1"
THRESHOLDS_SCHEMA = "driftgate.thresholds/1"
DEFAULT_GATE_CONFIG = Path("gate/config.yaml")
DEFAULT_SERVING_CONFIG = Path("serving/config.yaml")
FORCE_FAIL_ENV = "DRIFTGATE_FORCE_FAIL"
EXIT_PASS, EXIT_FAIL, EXIT_ERROR = 0, 1, 2
OPS = {">=": operator.ge, "<=": operator.le, ">": operator.gt, "<": operator.lt}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


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
) -> dict:
    prompts = _jsonl(prompts_path)
    panel = _jsonl(panel_path)
    if budget < 1 or budget > len(prompts) or budget > len(panel):
        raise ValueError(
            f"budget {budget} outside 1..min({len(prompts)} prompts, {len(panel)} panel items)"
        )
    props = server.props()
    t0 = time.monotonic()
    d3_run = d3.measure(server, prompts[:budget], seed)
    t1 = time.monotonic()
    d5_run = d5.measure(server, panel[:budget], seed)
    t2 = time.monotonic()
    return {
        "schema": MEASUREMENT_SCHEMA,
        "budget": budget,
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


def detectors(reference: dict, candidate: dict) -> dict:
    _check_pair(reference, candidate)
    return {
        "D3": d3.metrics(reference["d3"], candidate["d3"]),
        "D5": d5.metrics(reference["d5"], candidate["d5"]),
        "D6": d6.metrics(reference["d3"], candidate["d3"]),
    }


# ---------- decision ----------


def resolve_mode(mode: str, thresholds_path: Path) -> str:
    if mode == "auto":
        return "enforce" if thresholds_path.exists() else "report"
    if mode not in ("report", "enforce"):
        raise ValueError(f"unknown gate mode {mode!r}")
    return mode


def _lookup(obj: dict, dotted: str):
    for key in dotted.split("."):
        obj = obj[key]
    return obj


def evaluate(dets: dict, thresholds: dict) -> list[dict]:
    """Apply every rule in a thresholds file; each result says whether that rule held."""
    if thresholds.get("schema") != THRESHOLDS_SCHEMA:
        raise ValueError(f"unsupported thresholds schema {thresholds.get('schema')!r}")
    out = []
    for rule in thresholds["rules"]:
        value = _lookup(dets[rule["detector"]], rule["metric"])
        out.append(rule | {"observed": value, "ok": OPS[rule["op"]](value, rule["value"])})
    return out


def decide(dets: dict, mode: str, thresholds_path: Path, env=None) -> dict:
    env = os.environ if env is None else env
    effective = resolve_mode(mode, thresholds_path)
    forced = env.get(FORCE_FAIL_ENV) == "1"
    rules = None
    if effective == "enforce":
        if not thresholds_path.exists():
            raise ValueError(f"enforce mode needs {thresholds_path}")
        rules = evaluate(dets, json.loads(thresholds_path.read_text()))
    passed = None if rules is None else all(r["ok"] for r in rules)
    fail = forced or passed is False
    return {
        "mode": effective,
        "configured_mode": mode,
        "thresholds_file": str(thresholds_path) if thresholds_path.exists() else None,
        "rules": rules,
        "passed": passed,
        "forced_fail": forced,
        "exit_code": EXIT_FAIL if fail else EXIT_PASS,
    }


def verdict(reference: dict, candidate: dict, gate_config: dict, env=None) -> dict:
    t0 = time.monotonic()
    dets = detectors(reference, candidate)
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
    for name in ("measure", "compare", "run"):
        s = sub.add_parser(name)
        s.add_argument("--gate-config", type=Path, default=DEFAULT_GATE_CONFIG)
        s.add_argument("--out", type=Path, help="write JSON here (default: stdout)")
        if name in ("measure", "run"):
            s.add_argument("--config", type=Path, default=DEFAULT_SERVING_CONFIG)
            s.add_argument("--server", help="base URL (default: host and port from --config)")
            s.add_argument("--budget", type=int, required=True)
            s.add_argument("--seed", type=int, required=True)
        if name in ("compare", "run"):
            s.add_argument("--reference", type=Path, help="default: gate config `reference`")
        if name == "compare":
            s.add_argument("--candidate", type=Path, required=True)
        if name == "run":
            s.add_argument("--measurement-out", type=Path, help="also save the raw measurement")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        gate_config = load_yaml(args.gate_config)
        if args.cmd in ("measure", "run"):
            url = args.server or server_url(load_yaml(args.config))
            m = measure(
                LlamaServer(url),
                args.budget,
                args.seed,
                Path(gate_config["data"]["d3_prompts"]),
                Path(gate_config["data"]["d5_panel"]),
                args.config,
            )
            if args.cmd == "measure":
                _write(m, args.out)
                return EXIT_PASS
            if args.measurement_out:
                _write(m, args.measurement_out)
            candidate = m
        else:
            candidate = json.loads(args.candidate.read_text())
        ref_path = args.reference or Path(gate_config["reference"])
        v = verdict(json.loads(ref_path.read_text()), candidate, gate_config)
    except (OSError, ValueError, KeyError) as e:
        sys.stderr.write(f"gate: {type(e).__name__}: {e}\n")
        return EXIT_ERROR
    _write(v, args.out)
    return v["decision"]["exit_code"]
