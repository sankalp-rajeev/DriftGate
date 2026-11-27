"""Semantic drift from cosine similarity of paired D3 continuations."""

from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from gate import models, serve
from gate.llama_client import LlamaServer, RequestError, detector_error
from gate.manifest import load_yaml, sha256_file
from gate.stats import mean, percentile

REFERENCE_FILE = "d7_embeddings.json"
REFERENCE_SCHEMA = "driftgate.d7-reference/1"
METRIC = "semantic_drift"


def model_entry(config: dict) -> tuple[dict, dict]:
    catalogue = load_yaml(Path(config["models"]))
    entry = next(row for row in catalogue["models"] if row["role"] == "embedding")
    return catalogue, entry


@contextmanager
def embedding_server(config: dict):
    """Serve the verified embedding model on loopback only for the embedding pass."""
    catalogue, entry = model_entry(config)
    model = Path(catalogue["models_dir"]) / entry["file"]
    if sha256_file(model) != entry["sha256"]:
        raise ValueError(f"SHA-256 mismatch for {model}")
    bin_dir = Path(os.environ.get("DRIFTGATE_EMBEDDING_BIN", serve.llama_bin_dir()))
    lock = load_yaml(Path(config["llama_cpp_lock"]))
    if not serve.engine_matches(lock, serve.binary_commit(bin_dir)):
        raise ValueError(f"D7 binary in {bin_dir} does not match {config['llama_cpp_lock']}")

    argv = [
        str(bin_dir / "llama-server"), "--model", str(model),
        "--host", "127.0.0.1", "--port", str(config["port"]),
        "--embeddings", "--pooling", "cls", "--n-gpu-layers", "0",
        "--threads", str(config["threads"]), "--parallel", "1",
    ]  # fmt: skip
    server = LlamaServer(f"http://127.0.0.1:{config['port']}", timeout=config["timeout_s"])
    with (
        tempfile.TemporaryFile() as log,
        subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT) as proc,
    ):
        try:
            deadline = time.monotonic() + config["timeout_s"]
            while proc.poll() is None and time.monotonic() < deadline:
                try:
                    health = LlamaServer(server.base_url, timeout=0.5)
                    if health.health().get("status") == "ok":
                        props = health.props()
                        if props.get("model_path") != str(model):
                            raise ValueError(f"D7 port {config['port']} belongs to another server")
                        break
                except OSError:
                    time.sleep(0.1)
            else:
                log.seek(0)
                details = log.read().decode(errors="replace").splitlines()[-5:]
                raise ValueError(f"D7 embedding server did not become healthy: {' '.join(details)}")
            yield server.embedding
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()


def measure(continuations: list[dict], embed: Callable[[str], list[float]]) -> list[dict]:
    """Embed each nonblank D3 answer once and retain errors at the originating prompt."""
    rows = []
    for row in continuations:
        result = {"id": row["id"]}
        if "error" in row:
            result["error"] = row["error"]
        else:
            try:
                text = row["text"]
                vector = embed(text) if text.strip() else None
                if vector is not None and (
                    not vector or not all(math.isfinite(x) for x in vector) or not any(vector)
                ):
                    raise RequestError(None, "invalid embedding vector", "response")
                result["embedding"] = vector
            except RequestError as exc:
                result["error"] = exc.details
            except OSError as exc:
                result["error"] = RequestError(None, str(exc), "transport").details
        rows.append(result)
        if "error" in result:
            break
    return rows


def _similarity(reference: list[float] | None, candidate: list[float] | None) -> float:
    if reference == candidate:
        return 1.0
    if reference is None or candidate is None:
        return 0.0
    dot = math.fsum(a * b for a, b in zip(reference, candidate, strict=True))
    cosine = dot / (math.hypot(*reference) * math.hypot(*candidate))
    return max(-1.0, min(1.0, cosine))


def metrics(reference: list[dict], candidate: list[dict]) -> dict:
    if error := detector_error(candidate):
        return error
    if not candidate:
        raise ValueError("D7 has no candidate continuations")
    refs = {r["id"]: r["embedding"] for r in reference}
    per_prompt = [
        {"id": c["id"], "similarity": _similarity(refs[c["id"]], c["embedding"])} for c in candidate
    ]
    similarities = [row["similarity"] for row in per_prompt]
    p5 = percentile(similarities, 5)
    return {
        "n_prompts": len(per_prompt),
        "mean_similarity": mean(similarities),
        "p5_similarity": p5,
        METRIC: 1 - p5,
        "per_prompt": per_prompt,
    }


def capture(continuations: list[dict], config: dict) -> dict:
    """Store the encoder identity, prompt-aligned vectors and incremental wall time."""
    started = time.monotonic()
    _, entry = model_entry(config)
    if all("error" in row or not row["text"].strip() for row in continuations):
        rows = measure(continuations, lambda _: None)
    else:
        try:
            with embedding_server(config) as embed:
                rows = measure(continuations, embed)
        except (OSError, ValueError) as exc:
            rows = [{
                "id": continuations[0]["id"],
                "error": RequestError(None, str(exc), "startup").details,
            }]  # fmt: skip
    return {"model_sha256": entry["sha256"], "rows": rows, "seconds": time.monotonic() - started}


def reference_document(measurement: dict, key: str) -> dict:
    data = measurement["d7"]
    if not data["rows"] or detector_error(data["rows"]):
        raise ValueError("D7 reference contains no usable measurement")
    return {
        "schema": REFERENCE_SCHEMA,
        "key": key,
        "seed": measurement["seed"],
        "prompts_sha256": measurement["inputs"]["d3_prompts_sha256"],
        **data,
    }


def load_reference(path: Path) -> dict:
    measurement = json.loads(path.read_text())
    sidecar = path.with_name(REFERENCE_FILE)
    if path.name == "measurement.json" and sidecar.exists():
        reference = json.loads(sidecar.read_text())
        if (
            reference["schema"] != REFERENCE_SCHEMA
            or reference["key"] != measurement["reference"]["key"]
            or reference["seed"] != measurement["seed"]
            or reference["prompts_sha256"] != measurement["inputs"]["d3_prompts_sha256"]
            or [row["id"] for row in reference["rows"]]
            != [row["id"] for row in measurement["d3"][: len(reference["rows"])]]
        ):
            raise ValueError(f"D7 embeddings do not match the reference at {path}")
        measurement["d7"] = reference
    return measurement


def install_model(config: dict) -> dict:
    catalogue, entry = model_entry(config)
    result = models.ensure(catalogue, {"artifacts": {}}, entry)
    if result["status"] == "mismatch":
        raise ValueError(f"SHA-256 mismatch for {entry['file']}")
    return result
