"""Run the sampled D7 study on WSL and commit fitted thresholds before any faults."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from analysis import calibrate
from analysis.d7_inputs import encode, write_json
from faults import aa
from gate import d7, serve
from gate.llama_client import LlamaServer, RequestError, detector_error, user_prompt
from gate.manifest import load_yaml, sha256_file
from gate.stats import mean

REFERENCE_SEEDS = (1, 2, 3, 4, 5)
FIT_SEEDS = (6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25)
HELD_OUT_SEEDS = (26, 27, 28, 29, 30, 31, 32, 33, 34, 35)
FAULT_SEEDS = (36, 37)
METRICS = ("d7", "exact_match")
DEFAULTS = {"temperature": 0.8, "top_k": 40, "top_p": 0.95, "min_p": 0.05}
BRANCH = "feat/post-demo"
ORIGIN = "https://github.com/sankalp-rajeev/DriftGate.git"
PREREG = Path("docs/prereg/2026-11-26-d7-sampled.md")
REPORT = Path("docs/results/2026-11-26-d7-sampled.md")
STUDY = Path("runs/2026-11-19-study")
BUNDLES = Path("artifacts/bundles")


def sample(server: LlamaServer, prompts: list[dict], seed: int, sampler: dict) -> list[dict]:
    """Keep sampling parameters inherited from the running bundle, including F6's temperature."""
    rows = []
    for prompt in prompts:
        pid = prompt["id"]
        try:
            response = server.completion(
                user_prompt(server, prompt["instruction"]),
                n_predict=128,
                seed=seed,
                cache_prompt=False,
            )
        except RequestError as exc:
            rows.append({"id": pid, "error": exc.details})
            break
        settings = response["generation_settings"]
        for key in DEFAULTS:
            if not math.isclose(settings[key], sampler[key], abs_tol=1e-6):
                raise ValueError(f"serving sampler did not use {key}={sampler[key]}")
        rows.append({"id": pid, "text": response["content"]})
    return rows


def make_reference(samples: list[list[dict]]) -> list[dict]:
    if len(samples) != len(REFERENCE_SEEDS) or any(detector_error(rows) for rows in samples):
        raise ValueError("all five reference samples must complete")
    reference = []
    for group in zip(*samples, strict=True):
        pid = group[0]["id"]
        if any(row["id"] != pid for row in group):
            raise ValueError("reference prompt IDs do not match")
        vectors = [row["embedding"] for row in group if row["embedding"] is not None]
        centroid = (
            [math.fsum(values) / len(group) for values in zip(*vectors, strict=True)]
            if vectors
            else None
        )
        if centroid is not None and not any(centroid):
            raise ValueError(f"zero reference centroid for {pid}")
        reference.append(
            {"id": pid, "embedding": centroid, "texts": [row["text"] for row in group]}
        )
    return reference


def statistics(reference: list[dict], candidate: list[dict]) -> dict:
    if error := detector_error(candidate):
        return error
    if Counter(row["id"] for row in reference) != Counter(row["id"] for row in candidate):
        raise ValueError("candidate coverage differs from the reference")
    semantic = d7.metrics(reference, candidate)
    texts = {row["id"]: row["texts"] for row in reference}
    agreement = mean([row["text"] in texts[row["id"]] for row in candidate])
    return {
        "d7": 1 - semantic["mean_similarity"],
        "exact_match": 1 - agreement,
        "mean_similarity": semantic["mean_similarity"],
        "exact_match_agreement": agreement,
    }


def detected(result: dict, name: str, thresholds: dict) -> bool:
    return result.get("status") == "error" or result[name] > thresholds[name]


def calibration(fit: list[dict], held: list[dict]) -> dict:
    if len(fit) != len(FIT_SEEDS) or len(held) != len(HELD_OUT_SEEDS):
        raise ValueError("sampled calibration requires 20 fit and 10 held-out runs")
    if any(row.get("status") == "error" for row in fit):
        raise ValueError("a sampled A/A fit run failed")
    thresholds = {
        name: calibrate.threshold_value("upper", [row[name] for row in fit]) for name in METRICS
    }
    false_alarms = {}
    for name in METRICS:
        k = sum(detected(row, name, thresholds) for row in held)
        false_alarms[name] = {
            "k": k,
            "n": len(held),
            "ci95": calibrate.clopper_pearson(k, len(held)),
        }
    return {
        "thresholds": thresholds,
        "held_out_false_alarms": false_alarms,
        "fit": fit,
        "held": held,
    }


def bundle_sources() -> dict[str, tuple[Path, Path]]:
    baseline_bin = serve.llama_bin_dir()
    sources = {
        name: (BUNDLES / name, baseline_bin) for name in ["AA", *(f"F{i}" for i in range(1, 10))]
    }
    original = json.loads((STUDY / "faults/F8-downgrade-v1.0.0/f8.json").read_text())
    sources["F8"] = (BUNDLES / "F8", Path(original["bin_dir"]))
    repaired = json.loads((STUDY / "f8.json").read_text())
    sources["F8-corrected"] = (STUDY / "bundles/F8", Path(repaired["bin_dir"]))
    return sources


def verify_sources(sources: dict[str, tuple[Path, Path]]) -> dict:
    info = {}
    versions = {path: serve.binary_version(path) for path in {p for _, p in sources.values()}}
    engine_lock = "serving/llama_cpp.lock"
    original_f8 = json.loads((STUDY / "faults/F8-downgrade-v1.0.0/f8.json").read_text())
    aa_lock_path = sources["AA"][0] / engine_lock
    aa_lock = load_yaml(aa_lock_path)
    for name, (source, binary) in sources.items():
        metadata = json.loads((source / "bundle.json").read_text())
        lock = load_yaml(source / engine_lock)
        for file, digest in metadata["files"].items():
            if sha256_file(source / file) == digest:
                continue
            # The original F8 runner changed the lock commit after writing bundle.json.
            if (
                name == "F8"
                and file == engine_lock
                and digest == sha256_file(aa_lock_path)
                and lock == aa_lock | {"commit": original_f8["commit"]}
            ):
                continue
            raise ValueError(f"{source / file} differs from the original study bundle")
        if not serve.engine_matches(lock, serve.version_commit(versions[binary])):
            raise ValueError(f"{name} is missing the engine pinned in its bundle")
        config = load_yaml(source / "serving/config.yaml")
        sampler = {key: config["sampler"][key] for key in DEFAULTS}
        expected = DEFAULTS | ({"temperature": 1.3} if name == "F6" else {})
        if sampler != expected:
            raise ValueError(f"{name} sampler differs from the study protocol")
        info[name] = {
            "source": str(source), "commit": lock["commit"], "sampler": sampler,
            "bundle_sha256": sha256_file(source / "bundle.json"),
            "engine_lock_sha256": sha256_file(source / engine_lock),
        }  # fmt: skip
    return info


@contextmanager
def bundle_server(bundle: Path, binary: Path) -> Iterator[LlamaServer]:
    original = os.environ.get("LLAMA_CPP_BIN")
    os.environ["LLAMA_CPP_BIN"] = str(binary)
    try:
        proc, base = aa.start_server(bundle, Path("models"))
    finally:
        if original is None:
            del os.environ["LLAMA_CPP_BIN"]
        else:
            os.environ["LLAMA_CPP_BIN"] = original
    try:
        yield LlamaServer(base)
    finally:
        aa.stop_server(proc)


def generate(
    out: Path,
    name: str,
    stage: str,
    seeds: tuple[int, ...],
    source: tuple[Path, Path],
    prompts: list[dict],
    config: dict,
) -> Iterator[tuple[int, list[dict]]]:
    original, binary = source
    bundle = out / "bundles" / name
    if not bundle.exists():
        shutil.copytree(original, bundle, ignore=shutil.ignore_patterns("server.log"))
    sampler = load_yaml(bundle / "serving/config.yaml")["sampler"]
    for seed in seeds:
        print(f"{stage} {name} seed {seed}", flush=True)
        with bundle_server(bundle, binary) as server:
            rows = sample(server, prompts, seed, sampler)
        path = out / stage / name / f"seed-{seed:02d}.json"
        record = {"bundle": name, "seed": seed, "sampler": sampler, "rows": rows}
        write_json(path, record)
        texts = list(dict.fromkeys(row["text"] for row in rows if row.get("text", "").strip()))
        vectors = encode(texts, config)
        embeddings = d7.measure(rows, vectors.__getitem__)
        write_json(path, record | {"embeddings": embeddings})
        yield seed, [row | emb for row, emb in zip(rows, embeddings, strict=True)]


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def check_branch(expected: str | None = None) -> str:
    head = git("rev-parse", "HEAD")
    if git("branch", "--show-current") != BRANCH or git("remote", "get-url", "origin") != ORIGIN:
        raise ValueError(f"run the study on {BRANCH} with the GitHub origin")
    if expected is not None and head != expected:
        raise ValueError("branch HEAD changed while the sampled study was running")
    return head


def commit_document(path: Path, content: str, message: str, expected: str) -> str:
    check_branch(expected)
    with path.open("x") as document:
        document.write(content)
    git("add", "--", str(path))
    git("commit", "--only", "-m", message, "--", str(path))
    head = check_branch()
    if git("show", f"{head}:{path}") != content.strip():
        raise ValueError(f"{path} was not committed exactly")
    return head


def push(head: str) -> None:
    """Publish the exact experiment commit to the GitHub feature branch."""
    try:
        subprocess.run(
            ["git", "push", ORIGIN, f"{head}:refs/heads/{BRANCH}"],
            check=True,
            timeout=60,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"GitHub push failed for {head}: {exc}", flush=True)


def preregistration(out: Path, source_commit: str, fitted: dict) -> str:
    thresholds = fitted["thresholds"]
    return f"""# Sampled D7 pre-registration, 26 Nov 2026

Study implementation commit `{source_commit}`. Run directory `{out}`.

Use the first 16 D3 prompts in file order and at most 128 new tokens. The baseline
serving defaults are temperature 0.8, top-k 40, top-p 0.95 and min-p 0.05.
Inherit each bundle's defaults, including F6's changed temperature. The request
uses the stated seed and disables the prompt cache. The same two fault seeds
apply to every bundle. The original F8 v1.0.0 pair and corrected b12557 pair
are reported separately.

| Stage | Seeds |
| --- | --- |
| Reference | {", ".join(map(str, REFERENCE_SEEDS))} |
| A/A fit | {", ".join(map(str, FIT_SEEDS))} |
| A/A held out | {", ".join(map(str, HELD_OUT_SEEDS))} |
| Each fault | {", ".join(map(str, FAULT_SEEDS))} |

For each prompt, embed the five reference completions with the pinned BGE GGUF
and form their arithmetic centroid. Blank text contributes zero to the centroid.
When all five are blank, the reference and candidate score similarity 1 if both
are blank and 0 if just one is blank. Otherwise use cosine with the centroid.
D7 drift is 1 minus the mean similarity over the 16 prompts. Exact-match
agreement is the share of candidate texts that equal any of the five raw
reference texts for that prompt. Its drift statistic is 1 minus agreement.

Fit the upper cutoff as the maximum over the 20 fit runs. A run fires only when
its drift exceeds the cutoff. Generation request errors stop fitting and count
as detections for held-out and fault runs. Use the same outputs for both detectors.

| Detector | Fitted upper cutoff |
| --- | ---: |
| D7 | {thresholds["d7"]!r} |
| Exact match | {thresholds["exact_match"]!r} |

The local `calibration.json` has SHA-256
`{sha256_file(out / "calibration.json")}`. The local `reference.json` has SHA-256
`{sha256_file(out / "reference.json")}`. This commit precedes fault generation
and scoring. The experiment changes neither cutoff after this commit.
"""


def report(out: Path, fitted: dict, results: dict, prereg_commit: str) -> str:
    lines = [
        "# Sampled D7 study results",
        "",
        f"WSL run `{out}`. Thresholds committed in `{prereg_commit}`.",
        "",
        "| Detector | Fitted drift threshold | Held-out false alarms | Exact 95% interval |",
        "| --- | ---: | ---: | ---: |",
    ]
    for key, label in zip(METRICS, ("D7", "Exact match"), strict=True):
        alarms = fitted["held_out_false_alarms"][key]
        lo, hi = alarms["ci95"]
        lines.append(
            f"| {label} | {fitted['thresholds'][key]:.8g} | {alarms['k']}/{alarms['n']} |"
            f" {lo:.2%}–{hi:.2%} |"
        )
    lines += [
        "",
        "| Fault | D7 detections | Exact-match detections |",
        "| --- | ---: | ---: |",
    ]
    for fault, data in results.items():
        cells = [f"{data['detections'][key]}/{len(data['runs'])}" for key in METRICS]
        lines.append(f"| {fault} | {' | '.join(cells)} |")
    return "\n".join(lines) + "\n"


def run(out: Path, sources: dict[str, tuple[Path, Path]], info: dict) -> None:
    head = check_branch()
    if PREREG.exists() or REPORT.exists() or any(p.name != "console.log" for p in out.iterdir()):
        raise ValueError("the sampled study needs a new run directory and pre-registration")
    config = load_yaml(Path("gate/config.yaml"))
    prompts_path = Path(config["data"]["d3_prompts"])
    prompts = [json.loads(line) for line in prompts_path.read_text().splitlines()][:16]
    if len(prompts) != 16:
        raise ValueError("expected the 16 L1 D3 prompts")
    encoder = config["d7"]
    _, model = d7.model_entry(encoder)
    write_json(out / "run.json", {
        "commit": head, "bundles": info, "prompts_sha256": sha256_file(prompts_path),
        "embedding_model_sha256": model["sha256"],
    })  # fmt: skip
    refs = generate(out, "AA", "reference", REFERENCE_SEEDS, sources["AA"], prompts, encoder)
    reference = make_reference([rows for _, rows in refs])
    write_json(out / "reference.json", {"prompts": reference, "seeds": REFERENCE_SEEDS})

    def aa_statistics(stage: str, seeds: tuple[int, ...]) -> list[dict]:
        return [
            {"seed": seed} | statistics(reference, rows)
            for seed, rows in generate(out, "AA", stage, seeds, sources["AA"], prompts, encoder)
        ]

    fit = aa_statistics("aa-fit", FIT_SEEDS)
    held = aa_statistics("aa-held-out", HELD_OUT_SEEDS)
    fitted = calibration(fit, held)
    write_json(out / "calibration.json", fitted)
    content = preregistration(out, head, fitted)
    frozen = commit_document(PREREG, content, "D7: preregister sampled drift thresholds", head)
    write_json(out / "preregistration.json", {"commit": frozen, "sha256": sha256_file(PREREG)})
    push(frozen)

    results = {}
    for fault, source in sources.items():
        if fault == "AA":
            continue
        if git("show", f"{frozen}:{PREREG}") != content.strip():
            raise ValueError("sampled fault phase requires the committed thresholds")
        rows = [
            {"seed": seed} | statistics(reference, measured)
            for seed, measured in generate(
                out, fault, "faults", FAULT_SEEDS, source, prompts, encoder
            )
        ]
        counts = {
            key: sum(detected(row, key, fitted["thresholds"]) for row in rows) for key in METRICS
        }
        results[fault] = {"runs": rows, "detections": counts}
        write_json(out / "fault-scores.json", results)
    summary = report(out, fitted, results, frozen)
    (out / "report.md").write_text(summary)
    report_commit = commit_document(
        REPORT, summary, "D7: report the sampled decoding study", frozen
    )
    push(report_commit)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--check", action="store_true", help="verify archived inputs without serving"
    )
    args = parser.parse_args()
    check_branch()
    sources = bundle_sources()
    info = verify_sources(sources)
    if args.check:
        print(json.dumps(info, indent=2))
        return
    args.out.mkdir(parents=True, exist_ok=True)
    run(args.out, sources, info)


if __name__ == "__main__":
    main()
