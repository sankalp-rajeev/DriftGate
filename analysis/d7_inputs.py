"""Prepare archived D3 continuations for offline D7 scoring."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from gate import d7, serve
from gate.llama_client import LlamaServer
from gate.manifest import load_manifest, load_yaml, sha256_file

STUDY = Path("runs/2026-11-19-study")
OUT = Path("runs/2026-11-26-d7-offline")
ARCHIVE = Path("results/2026-11-19-study")
PREREG = Path("docs/prereg/2026-11-26-d7-offline.md")
REPAIR = Path("docs/results/f8-repair.json")
FAULTS = tuple(f"F{i}" for i in range(1, 10))
DOWNGRADE = "F8-downgrade-v1.0.0"


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    tmp.replace(path)


def reference_path() -> Path:
    base = load_yaml(Path("gate/baseline.yaml"))
    return (
        Path("artifacts/reference")
        / base["model_sha256"]
        / base["config_hash"]
        / "local-Sankalps-Razer"
        / "measurement.json"
    )


def archive_paths(stage: str) -> dict[str, Path]:
    if stage == "aa":
        return {"reference": reference_path()} | {
            f"aa/run-{i:02d}": STUDY / "aa" / f"run-{i:02d}.measurement.json" for i in range(30)
        }
    paths = {
        f"{fault}/run-{i:02d}": STUDY / "faults" / fault / f"run-{i:02d}.measurement.json"
        for fault in (*FAULTS, DOWNGRADE)
        for i in range(2)
    }
    repair = json.loads(REPAIR.read_text())
    for row in repair["corrected_runs"] + repair["archived_downgrade"]["runs"]:
        source = STUDY / row["source"]
        if sha256_file(source) != row["measurement_sha256"]:
            raise ValueError(f"{source} no longer matches the F8 repair record")
    return paths


def require_preregistration() -> dict:
    """Require the fitted A/A evidence and its checksum in a committed pre-registration."""
    fit = ARCHIVE / "d7-calibration.json"
    digest = sha256_file(fit)
    committed = subprocess.check_output(["git", "show", f"HEAD:{PREREG}"])
    if committed != PREREG.read_bytes() or digest not in committed.decode():
        raise ValueError("commit the D7 threshold and calibration checksum before accessing faults")
    doc = json.loads(fit.read_text())
    if not doc["levels"]:
        raise ValueError("D7 calibration has no fitted levels")
    return doc


@contextmanager
def tokenizer(config: dict):
    """Start a loopback-only tokenizer using the baseline model, and call only /detokenize."""
    catalogue = load_yaml(Path(config["models"]))
    baseline = load_yaml(Path("gate/baseline.yaml"))
    (name,) = [
        name
        for name, entry in load_manifest()["artifacts"].items()
        if entry["sha256"] == baseline["model_sha256"]
    ]
    model = Path(catalogue["models_dir"]) / name
    if sha256_file(model) != baseline["model_sha256"]:
        raise ValueError(f"{model} does not match the baseline SHA-256")
    bin_dir = serve.llama_bin_dir()
    lock = load_yaml(Path(config["llama_cpp_lock"]))
    if not serve.engine_matches(lock, serve.binary_commit(bin_dir)):
        raise ValueError("the tokenizer must use the pinned llama.cpp build")

    # Embedding mode disables the completion route. This helper sends only detokenize requests.
    argv = [
        str(bin_dir / "llama-server"), "--model", str(model), "--host", "127.0.0.1",
        "--port", str(config["detokenize_port"]), "--embeddings", "--pooling", "last",
        "--no-warmup", "--n-gpu-layers", "0", "--threads", str(config["threads"]),
        "--parallel", "1", "--ctx-size", "512",
    ]  # fmt: skip
    client = LlamaServer(f"http://127.0.0.1:{config['detokenize_port']}", timeout=2)
    with tempfile.TemporaryFile() as log:
        proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + config["timeout_s"]
            while proc.poll() is None and time.monotonic() < deadline:
                try:
                    client.detokenize([])
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                log.seek(0)
                raise ValueError(f"tokenizer failed to start: {log.read().decode()[-1500:]}")
            yield client.detokenize
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()


def encode(texts: list[str], config: dict) -> dict[str, list[float]]:
    """Encode each distinct nonblank text with the pinned standalone llama-embedding CLI."""
    if not texts:
        return {}
    catalogue, entry = d7.model_entry(config)
    model = Path(catalogue["models_dir"]) / entry["file"]
    if sha256_file(model) != entry["sha256"]:
        raise ValueError("embedding model SHA-256 does not match its pin")
    binary = serve.llama_bin_dir() / "llama-embedding"
    pin = load_yaml(Path(config["llama_cpp_lock"]))
    proc = subprocess.run([str(binary), "--version"], capture_output=True, text=True, check=True)
    if not serve.engine_matches(pin, serve.version_commit(proc.stdout + proc.stderr)):
        raise ValueError("llama-embedding does not match the pinned engine")
    separator = "\x1e"
    if any(separator in text for text in texts):
        raise ValueError("archived continuation contains the embedding separator")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as source:
        source.write(separator.join(texts))
        source.flush()
        result = subprocess.run(
            [
                str(binary), "--model", str(model), "--file", source.name,
                "--n-gpu-layers", "0", "--threads", str(config["threads"]),
                "--pooling", "cls", "--embd-output-format", "array", "--embd-normalize", "2",
                "--embd-separator", separator, "--batch-size", "512", "--ubatch-size", "512",
                "--ctx-size", "512", "--parallel", "1", "--no-warmup",
            ],
            capture_output=True, text=True, check=True,
        )  # fmt: skip
    vectors = json.loads(result.stdout)
    if len(vectors) != len(texts) or any(
        len(vector) != 384 or not any(vector) or not all(map(math.isfinite, vector))
        for vector in vectors
    ):
        raise ValueError("invalid BGE embeddings")
    return dict(zip(texts, vectors, strict=True))


def prepare(stage: str) -> None:
    if stage == "faults":
        require_preregistration()
    paths = archive_paths(stage)
    inputs = {}
    for name, path in paths.items():
        measurement = json.loads(path.read_text())
        rows = measurement["d3"]
        ids = [row["id"] for row in rows]
        if not rows or len(set(ids)) != len(ids) or measurement["seed"] != 0:
            raise ValueError(f"invalid D3 archive {path}")
        inputs[name] = (measurement, path, sha256_file(path))
    config = load_yaml(Path("gate/config.yaml"))["d7"]
    sequences = dict.fromkeys(
        tuple(row["tokens"])
        for measurement, _, _ in inputs.values()
        for row in measurement["d3"]
        if "error" not in row
    )
    with tokenizer(config) as decode:
        text_by_tokens = {tokens: decode(list(tokens)) for tokens in sequences}
    texts = list(dict.fromkeys(text for text in text_by_tokens.values() if text.strip()))
    previous = {}
    if stage == "faults":
        fit_path = OUT / "aa-inputs.json"
        fitted = require_preregistration()
        if sha256_file(fit_path) != fitted["aa_inputs_sha256"]:
            raise ValueError("A/A embeddings changed after the threshold was committed")
        previous = json.loads(fit_path.read_text())["vectors"]
    vectors = previous | encode([text for text in texts if text not in previous], config)
    _, model = d7.model_entry(config)
    runs = {}
    for name, (measurement, path, digest) in inputs.items():
        rows = [
            {"id": row["id"], "error": row["error"]}
            if "error" in row
            else {"id": row["id"], "text": text_by_tokens[tuple(row["tokens"])]}
            for row in measurement["d3"]
        ]
        runs[name] = {
            "source": str(path),
            "sha256": digest,
            "rows": rows,
            "embeddings": d7.measure(rows, vectors.__getitem__),
        }
    destination = OUT / f"{stage}-inputs.json"
    write_json(
        destination,
        {"embedding_model_sha256": model["sha256"], "runs": runs, "vectors": vectors},
    )
    receipt = {
        "embedding_model_sha256": model["sha256"],
        "inputs_sha256": sha256_file(destination),
        "unique_token_sequences": len(sequences),
        "unique_nonblank_texts": len(texts),
        "runs": {
            name: {"source": run["source"], "sha256": run["sha256"], "items": len(run["rows"])}
            for name, run in runs.items()
        },
    }
    write_json(ARCHIVE / f"d7-{stage}-inputs.json", receipt)
    print(f"Prepared {len(runs)} archived measurements with {len(texts)} distinct nonblank texts.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("aa", "faults"))
    prepare(parser.parse_args().stage)


if __name__ == "__main__":
    main()
