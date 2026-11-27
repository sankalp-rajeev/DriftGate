"""Run the D7 A/A calibration on t3.large or the D7 fault matrix on Ubuntu."""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from analysis import calibrate, study
from faults import aa, inject
from gate import cli, d7, serve
from gate import reference as ref_store
from gate.llama_client import LlamaServer, detector_error
from gate.manifest import load_yaml
from gate.score import validate

PREREG = Path("docs/prereg/2026-11-26-d7.md")


def write(path: Path, value: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2) + "\n")
    tmp.replace(path)


def build_reference(path: Path, source: Path, config: dict, bundle: Path, level: str) -> dict:
    """Replay the baseline's D3 answers and add embeddings to the archived measurement."""
    base = json.loads(source.read_text())
    if base["seed"] != 0 or any("error" in row for row in base["d3"]):
        raise ValueError("D7 needs a successful, seed-0 D3 reference")
    n3, n5 = calibrate.LEVELS[level]
    process, url = aa.start_server(bundle, Path("models"))
    try:
        measured = cli.measure(
            LlamaServer(url), n3, 0,
            Path(config["data"]["d3_prompts"]), Path(config["data"]["d5_panel"]),
            d5_budget=n5, d3_only=True, embedding_config=config["d7"],
        )  # fmt: skip
    finally:
        aa.stop_server(process)
    if error := detector_error(measured["d7"]["rows"]):
        raise ValueError(f"D7 reference failed: {error}")
    if measured["inputs"]["d3_prompts_sha256"] != base["inputs"]["d3_prompts_sha256"] or [
        (r["id"], r["tokens"]) for r in measured["d3"]
    ] != [(r["id"], r["tokens"]) for r in base["d3"][:n3]]:
        raise ValueError("baseline replay differs from the archived D3 reference")
    base["d7"] = measured["d7"]
    write(path, base)
    return base


def lab_calibration(reference: dict, runs: list[dict], thresholds: dict):
    n_cal, n_held = calibrate.N_CALIBRATION, calibrate.N_HELD_OUT
    if len(runs) != n_cal + n_held:
        raise ValueError(f"expected {n_cal + n_held} Lab A/A runs, found {len(runs)}")
    measurements = [cli.semantic_metrics(reference, run) for run in runs]
    calibration, held = measurements[:n_cal], measurements[n_cal:]
    if any(result.get("status") == "error" for result in calibration):
        raise ValueError("cannot calibrate D7 from detector errors")
    metric = d7.METRIC
    value = calibrate.threshold_value("upper", [result[metric] for result in calibration])
    k = sum(result.get("status") == "error" or result[metric] > value for result in held)
    timing = [row["embedding_seconds"] for row in measurements]
    eligible = k <= calibrate.MAX_FALSE_ALARMS
    report = {
        "level": "L1",
        "n_calibration": n_cal,
        "D7": {
            "k": k,
            "n": n_held,
            "rate": k / n_held,
            "ci95": calibrate.clopper_pearson(k, n_held),
        },
        "per_detector_bound": 1 / (n_cal + 1),
        "eligible": eligible,
        "embedding_seconds_mean": statistics.mean(timing),
        "embedding_seconds_max": max(timing),
    }
    if eligible:
        thresholds = thresholds | {"D7": thresholds["D7"] | {"value": value}}
        validate(thresholds)
    return report, thresholds


def run_lab(out: Path, source: Path, config: dict, s3_uri: str | None) -> None:
    level = "L1"
    if ref_store.instance_type() != "t3.large":
        raise ValueError("Lab D7 A/A requires a t3.large")
    serving = load_yaml(inject.REFERENCE_CONFIG)
    serving["server"]["port"] = config["d7"]["aa_generation_port"]
    serving_path = out / "serving.yaml"
    serving_path.write_text(yaml.safe_dump(serving, sort_keys=False))
    bundle = inject.build("AA", out / "bundles", base_config=serving_path)
    path = out / "reference.json"
    base = build_reference(path, source, config, bundle, level)
    aa.main(
        [
            "--runs",
            str(calibrate.N_CALIBRATION + calibrate.N_HELD_OUT),
            "--level",
            level,
            "--seed",
            "0",
            "--d3-only",
            "--bundle",
            str(bundle),
            "--reference",
            str(path),
            "--out",
            str(out / "aa"),
        ]  # fmt: skip
    )
    report, thresholds = lab_calibration(
        base,
        calibrate.load_runs(out / "aa"),
        json.loads(Path(config["thresholds"]).read_text()),
    )
    write(out / "calibration.json", report)
    if report["eligible"]:
        write(out / "thresholds.json", thresholds)
    key = base["reference"]["key"]
    embeddings = out / d7.REFERENCE_FILE
    write(embeddings, d7.reference_document(base, key))
    if s3_uri:
        ref_store.upload(embeddings, f"{s3_uri.rstrip('/')}/{key}{d7.REFERENCE_FILE}")


class MatrixStudy(study.Study):
    def __init__(self, out: Path, reference: Path, previous: Path):
        super().__init__(out, reference=reference, include_rq2=False)
        self.previous = previous

    def bundle(self, fault: str) -> Path:
        path = self.out / "bundles" / fault
        if not (path / "bundle.json").exists():
            inject.build(fault, path.parent)
        return path

    def f8_unit(self, cutoff=None) -> study.Unit:
        """Replay the originally reported F8 engine rather than selecting a later release."""
        marker = self.out / "f8.json"

        def prepare():
            original = json.loads((self.previous / "f8.json").read_text())
            if not serve.engine_matches(original, serve.binary_commit(Path(original["bin_dir"]))):
                raise ValueError("original F8 llama-server binary is unavailable")
            bundle = self.out / "bundles/F8"
            old = self.previous / "bundles/F8"
            shutil.copytree(
                old, bundle, dirs_exist_ok=True, ignore=shutil.ignore_patterns("server.log")
            )
            write(marker, original)
            self.f8_info()

        return study.Unit("f8-reuse", marker.exists, prepare)

    def determinism_unit(self) -> study.Unit:
        base = super().determinism_unit()

        def run():
            base.run()
            base_path = self.out / "determinism.json"
            runs = calibrate.load_runs(self.out / "aa")
            calibrate.calibrate(json.loads(self.reference.read_text()), runs)
            doc = json.loads(base_path.read_text())
            write(base_path, doc | {"d7_calibration_checked": True})

        def done():
            path = self.out / "determinism.json"
            return path.exists() and json.loads(path.read_text()).get("d7_calibration_checked")

        return study.Unit(base.name, done, run)

    def repetition_count(self) -> int:
        path = self.out / "determinism.json"
        if not json.loads(path.read_text()).get("d7_calibration_checked"):
            raise ValueError("D7 calibration must pass before fault trials")
        return super().repetition_count()


def run_matrix(out: Path, source: Path, config: dict, previous: Path) -> None:
    # The new pre-registration lives on this branch, alongside the original frozen protocol.
    committed = subprocess.check_output(["git", "show", f"HEAD:{PREREG}"])
    if (
        committed != PREREG.read_bytes()
        or study.sha256(study.FAULTS_FILE) not in committed.decode()
    ):
        raise ValueError(
            "commit the D7 pre-registration with the current fault hash before running"
        )
    ref_path = out / "reference.json"
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    write(out / "revision.json", {"commit": revision})
    runner = MatrixStudy(out, ref_path, previous)
    if not ref_path.exists():
        build_reference(ref_path, source, config, runner.bundle("AA"), "L3")
    result = study.execute(runner, datetime.now() + timedelta(days=7))
    if result != "complete":
        raise SystemExit(f"fault study {result}; see {out}/study.log")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="run", required=True)
    for name in ("lab-aa", "matrix"):
        command = sub.add_parser(name)
        command.add_argument("--out", type=Path, required=True)
        command.add_argument("--reference", type=Path, required=True)
        if name == "lab-aa":
            command.add_argument("--s3-uri")
            command.add_argument("--revision", required=True)
        else:
            command.add_argument("--previous", type=Path, required=True)
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    config = load_yaml(cli.DEFAULT_GATE_CONFIG)
    d7.install_model(config["d7"])
    if args.run == "lab-aa":
        write(args.out / "revision.json", {"commit": args.revision})
        run_lab(args.out, args.reference, config, args.s3_uri)
    else:
        run_matrix(args.out, args.reference, config, args.previous)


if __name__ == "__main__":
    main()
