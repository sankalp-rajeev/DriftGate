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

Local root, optional S3 URI and the baseline file come from `gate/config.yaml`
(`reference_store.local_root`, `.s3_uri`, `baseline`). Upload to S3 happens only with an
explicit `--upload`.

Which key does a candidate use? Always the baseline's. `gate/baseline.yaml` pins the model
SHA-256 and config hash of the approved reference release; only a human changes that file. The
runtime key is `reference/<baseline model sha>/<baseline config hash>/<instance-type>/`, so a
candidate (say F4, with its own config hash) is compared against the baseline's reference.
`--reference-key` overrides the key (tests, one-off comparisons).

`<instance-type>` is derived: IMDSv2 on EC2, `local-<hostname>` on a laptop. `--instance-type`
and `$DRIFTGATE_INSTANCE_TYPE` override it (tests).
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import socket
import subprocess
import urllib.request
from pathlib import Path

from gate.manifest import DEFAULT_MANIFEST, load_manifest, load_yaml

INSTANCE_ENV = "DRIFTGATE_INSTANCE_TYPE"
MEASUREMENT_FILE = "measurement.json"
D2_BASE_FILE = "d2_base.kld"
DEFAULT_BASELINE = Path("gate/baseline.yaml")
IMDS = "http://169.254.169.254/latest"


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


def imds_instance_type(timeout: float = 1.0) -> str | None:
    """EC2 instance type through IMDSv2 (token first), or None when not on EC2."""
    try:
        req = urllib.request.Request(
            f"{IMDS}/api/token",
            method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            token = r.read().decode()
        req = urllib.request.Request(
            f"{IMDS}/meta-data/instance-type", headers={"X-aws-ec2-metadata-token": token}
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode().strip() or None
    except (OSError, ValueError):
        return None


def instance_type(explicit: str | None = None, env=None, imds=None, hostname=None) -> str:
    """--instance-type, else $DRIFTGATE_INSTANCE_TYPE, else IMDSv2, else `local-<hostname>`."""
    env = os.environ if env is None else env
    value = explicit or env.get(INSTANCE_ENV) or (imds or imds_instance_type)()
    if not value:
        host = re.sub(r"[^A-Za-z0-9-]+", "-", hostname or socket.gethostname()).strip("-")
        value = f"local-{host or 'host'}"
    if not value.replace(".", "").replace("-", "").isalnum():
        raise ValueError(f"invalid instance type {value!r}")
    return value


def load_baseline(path: Path = DEFAULT_BASELINE) -> dict:
    """The approved reference release: `model_sha256` and `config_hash` (human-edited file)."""
    base = load_yaml(Path(path))
    for field in ("model_sha256", "config_hash"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(base.get(field, ""))):
            raise ValueError(f"{path}: {field} must be a SHA-256 hex digest")
    return base


def reference_key(serving: dict, instance: str, manifest_path: Path = DEFAULT_MANIFEST) -> str:
    return f"reference/{model_sha256(serving, manifest_path)}/{config_hash(serving)}/{instance}/"


def local_dir(gate_config: dict, key: str) -> Path:
    return Path(gate_config["reference_store"]["local_root"]) / key


def s3_uri(gate_config: dict, key: str, override: str | None = None) -> str | None:
    base = override or gate_config["reference_store"].get("s3_uri")
    return None if not base else f"{base.rstrip('/')}/{key}"


def upload(local: Path, uri: str, run=subprocess.run) -> None:
    """Copy one file to S3 (only called with an explicit --upload)."""
    run(["aws", "s3", "cp", "--only-show-errors", "--sse", "AES256", str(local), uri], check=True)


def resolve_key(
    gate_config: dict,
    serving: dict,
    instance: str | None = None,
    explicit_key: str | None = None,
    manifest_path: Path = DEFAULT_MANIFEST,
    imds=None,
) -> str:
    """Explicit key, else the baseline's key on this instance type. `serving` is not used: a
    candidate never picks its own reference."""
    if explicit_key:
        return explicit_key if explicit_key.endswith("/") else explicit_key + "/"
    base = load_baseline(gate_config.get("baseline", DEFAULT_BASELINE))
    where = instance_type(instance, imds=imds)
    return f"reference/{base['model_sha256']}/{base['config_hash']}/{where}/"


def is_baseline(gate_config: dict, serving: dict, manifest_path: Path = DEFAULT_MANIFEST) -> bool:
    """Whether `serving` is the baseline release (so it may write the baseline's reference)."""
    base = load_baseline(gate_config.get("baseline", DEFAULT_BASELINE))
    return (
        model_sha256(serving, manifest_path) == base["model_sha256"]
        and config_hash(serving) == base["config_hash"]
    )
