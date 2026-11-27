"""Fit or score D7 offline from archived D3 measurements and prepared embeddings."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from analysis import calibrate, matrix
from analysis.d7_inputs import (
    ARCHIVE,
    DOWNGRADE,
    FAULTS,
    OUT,
    STUDY,
    archive_paths,
    require_preregistration,
    write_json,
)
from gate import d7
from gate.manifest import sha256_file


def load_inputs(stage: str) -> dict:
    inputs = json.loads((OUT / f"{stage}-inputs.json").read_text())
    paths = archive_paths(stage)
    if set(inputs["runs"]) != set(paths):
        raise ValueError(f"unexpected {stage} input coverage")
    for name, path in paths.items():
        cached = inputs["runs"][name]
        measurement = json.loads(path.read_text())
        if (
            cached["source"] != str(path)
            or cached["sha256"] != sha256_file(path)
            or [row["id"] for row in measurement["d3"]] != [row["id"] for row in cached["rows"]]
        ):
            raise ValueError(f"prepared embeddings do not match archived measurement {path}")
    return inputs


def score_run(reference: dict, run: dict, level: str) -> dict:
    n = calibrate.LEVELS[level][0]
    rows = run["embeddings"][:n]
    if len(rows) != n and not any("error" in row for row in rows):
        raise ValueError(f"{run['source']} lacks coverage for {level}")
    result = d7.metrics(reference["embeddings"], rows)
    result.pop("per_prompt", None)
    return result


def fit_thresholds(inputs: dict) -> dict:
    runs = inputs["runs"]
    reference = runs["reference"]
    n_fit, n_held = calibrate.N_CALIBRATION, calibrate.N_HELD_OUT
    fit_ids = [f"aa/run-{i:02d}" for i in range(n_fit)]
    held_ids = [f"aa/run-{i:02d}" for i in range(n_fit, n_fit + n_held)]
    levels = {}
    for level in calibrate.LEVELS:
        fit = {name: score_run(reference, runs[name], level) for name in fit_ids}
        held = {name: score_run(reference, runs[name], level) for name in held_ids}
        if any("error" in result for result in fit.values()):
            raise ValueError(f"cannot fit D7 threshold from {level} detector errors")
        value = calibrate.threshold_value("upper", [result[d7.METRIC] for result in fit.values()])
        k = sum(
            "error" in r or calibrate.exceeds("upper", r[d7.METRIC], value) for r in held.values()
        )
        levels[level] = {
            "d3_prompts": calibrate.LEVELS[level][0],
            "threshold": value,
            "held_out": {
                "k": k,
                "n": n_held,
                "rate": k / n_held,
                "ci95": calibrate.clopper_pearson(k, n_held),
            },  # fmt: skip
            "fit_statistics": fit,
            "held_out_statistics": held,
        }
    return {
        "embedding_model_sha256": inputs["embedding_model_sha256"],
        "reference_sha256": reference["sha256"],
        "fit_runs": fit_ids,
        "held_out_runs": held_ids,
        "per_detector_bound": 1 / (n_fit + 1),
        "levels": levels,
    }


def score_faults(reference: dict, inputs: dict, fit: dict) -> dict:
    output = {}
    for name, run in inputs["runs"].items():
        levels = {}
        for level, info in fit["levels"].items():
            result = score_run(reference, run, level)
            detected = "error" in result or calibrate.exceeds(
                "upper", result[d7.METRIC], info["threshold"]
            )
            levels[level] = result | {"detected": detected}
        output[name] = {"source": run["source"], "sha256": run["sha256"], "levels": levels}
    return output


def extend_matrix(source: Path, target: Path, results: dict) -> None:
    """Retain legacy detector rows and add the D7 results at each fault and budget."""
    with source.open(newline="") as f:
        rows = list(csv.DictReader(f))
    rows = [row for row in rows if row["detector"] != "D7"]
    outputs = []
    for row in rows:
        fault, level = row["fault"], row["level"]
        if fault not in FAULTS:
            raise ValueError(f"unexpected fault {fault} in the published matrix")
        flags = [results[f"{fault}/run-{i:02d}"]["levels"][level]["detected"] for i in range(2)]
        tally = f"{sum(flags)}/{len(flags)}"
        if row["detector"] == "gate":
            if any(flags):
                legacy = calibrate.load_runs(STUDY / "faults" / fault)
                reference = json.loads(archive_paths("aa")["reference"].read_text())
                rules = json.loads(
                    (STUDY / "calibration" / f"thresholds-{level}.json").read_text()
                )["rules"]
                hits = [
                    any(calibrate.fired(
                        calibrate.statistics(reference, run, level), rules, run.get("d1_ok")
                    ).values())
                    for run in legacy
                ]  # fmt: skip
                count = sum(old or new for old, new in zip(hits, flags, strict=True))
                lo, hi = calibrate.clopper_pearson(count, len(flags))
                row |= {"detections": count, "rate": count / len(flags),
                        "ci95_low": lo, "ci95_high": hi}  # fmt: skip
            k, n = sum(flags), len(flags)
            lo, hi = calibrate.clopper_pearson(k, n)
            outputs.append(
                row
                | {
                    "detector": "D7",
                    "runs": n,
                    "detections": k,
                    "rate": k / n,
                    "ci95_low": lo,
                    "ci95_high": hi,
                    "D7": tally,
                }  # fmt: skip
            )
        outputs.append(row | {"D7": tally})
    tmp = target.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[*matrix.COLUMNS, "D7"])
        writer.writeheader()
        writer.writerows(outputs)
    tmp.replace(target)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("fit", "score", "matrix"))
    stage = parser.parse_args(argv).stage
    if stage == "fit":
        result = fit_thresholds(load_inputs("aa"))
        result["aa_inputs_sha256"] = sha256_file(OUT / "aa-inputs.json")
        write_json(ARCHIVE / "d7-calibration.json", result)
    elif stage == "score":
        fit = require_preregistration()
        aa = load_inputs("aa")
        faults = load_inputs("faults")
        if (
            sha256_file(OUT / "aa-inputs.json") != fit["aa_inputs_sha256"]
            or faults["embedding_model_sha256"] != aa["embedding_model_sha256"]
        ):
            raise ValueError("D7 embeddings differ from the calibrated inputs")
        scores = score_faults(aa["runs"]["reference"], faults, fit)
        write_json(ARCHIVE / "d7-faults.json", scores)
    else:
        require_preregistration()
        scores = json.loads((ARCHIVE / "d7-faults.json").read_text())
        if not all(f"{DOWNGRADE}/run-{i:02d}" in scores for i in range(2)):
            raise ValueError("the F8 downgrade scores are missing")
        target = ARCHIVE / "matrix.csv"
        extend_matrix(target, target, scores)
        extend_matrix(STUDY / "matrix.csv", STUDY / "matrix.csv", scores)


if __name__ == "__main__":
    main()
