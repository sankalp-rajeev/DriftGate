"""Reference key and reference store (PRD Section 6.8, T-32).

A reference measurement (D3, D5, D6 raw outputs, `python -m gate reference`) and the D2 reference
logits (`python -m gate.d2 base`) are stored under one key:

    reference/<model-sha256>/<config-hash>/<instance-type>/
        measurement.json      gate reference
        d2_base.kld           gate.d2 base

* `<model-sha256>`: the served model's SHA-256 from `serving/manifest.json`.
* `<config-hash>`: SHA-256 of the *effective serving config*, the settings that can change model
  output, as canonical JSON (keys sorted, separators `,` and `:`, UTF-8). It is the serving
  config with
    - `server.host`, `server.port` and `server.metrics` removed (where it listens does not
      change what it says; instances bind 0.0.0.0, laptops 127.0.0.1),
    - `models` removed (a path to the catalogue, not a setting),
    - `model` removed (the model is already in the key as `<model-sha256>`),
    - `chat_template` replaced by the SHA-256 of the template file's bytes,
    - `llama_cpp_lock` replaced by the pinned llama.cpp commit from that file.
  So two configs that differ only in comments, layout, port or host share a key; a changed
  template, KV-cache type, context, thread count, sampler default or engine commit does not.
* `<instance-type>`: `--instance-type`, else `$DRIFTGATE_INSTANCE_TYPE`, else `local`.

Local root, optional S3 URI and an optional pinned key come from `gate/config.yaml`
(`reference_store.local_root`, `.s3_uri`, `.key`). Upload to S3 happens only with an explicit
`--upload`.

Which key does a candidate use? A candidate (say F4) has its own config hash, so comparing it
with "the key of its own config" would find nothing. `reference_store.key` (or
`--reference-key`) pins the reference revision's key; when it is null the key is computed from
the serving config given, which is right for the reference revision itself and for A/A runs.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
from pathlib import Path

from gate.manifest import DEFAULT_MANIFEST, load_manifest, load_yaml

INSTANCE_ENV = "DRIFTGATE_INSTANCE_TYPE"
MEASUREMENT_FILE = "measurement.json"
D2_BASE_FILE = "d2_base.kld"


def effective_config(serving: dict) -> dict:
    eff = copy.deepcopy(serving)
    for key in ("host", "port", "metrics"):
        eff.get("server", {}).pop(key, None)
    eff.pop("models", None)
    eff.pop("model", None)
    eff["chat_template"] = hashlib.sha256(Path(serving["chat_template"]).read_bytes()).hexdigest()
    eff["llama_cpp_lock"] = load_yaml(Path(serving["llama_cpp_lock"]))["commit"]
    return eff


def config_hash(serving: dict) -> str:
    canonical = json.dumps(effective_config(serving), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def model_sha256(serving: dict, manifest_path: Path = DEFAULT_MANIFEST) -> str:
    entry = load_manifest(manifest_path)["artifacts"].get(serving["model"])
    if entry is None:
        raise ValueError(f"{serving['model']} is not in {manifest_path}")
    return entry["sha256"]


def instance_type(explicit: str | None = None, env=None) -> str:
    env = os.environ if env is None else env
    value = explicit or env.get(INSTANCE_ENV) or "local"
    if not value.replace(".", "").replace("-", "").isalnum():
        raise ValueError(f"invalid instance type {value!r}")
    return value


def reference_key(serving: dict, instance: str, manifest_path: Path = DEFAULT_MANIFEST) -> str:
    return f"reference/{model_sha256(serving, manifest_path)}/{config_hash(serving)}/{instance}/"


def local_dir(gate_config: dict, key: str) -> Path:
    return Path(gate_config["reference_store"]["local_root"]) / key


def s3_uri(gate_config: dict, key: str, override: str | None = None) -> str | None:
    base = override or gate_config["reference_store"].get("s3_uri")
    return None if not base else f"{base.rstrip('/')}/{key}"


def upload(local: Path, uri: str, run=subprocess.run) -> None:
    """Copy one file to S3 (only called with an explicit --upload)."""
    run(["aws", "s3", "cp", "--only-show-errors", str(local), uri], check=True)


def resolve_key(
    gate_config: dict,
    serving: dict,
    instance: str | None = None,
    explicit_key: str | None = None,
    manifest_path: Path = DEFAULT_MANIFEST,
) -> str:
    """Explicit key, else the pinned `reference_store.key`, else the key of `serving`."""
    key = explicit_key or gate_config["reference_store"].get("key")
    if key:
        return key if key.endswith("/") else key + "/"
    return reference_key(serving, instance_type(instance), manifest_path)
