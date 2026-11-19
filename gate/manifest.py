"""SHA-256 manifest of model artifacts (FR-02).

The manifest (`serving/manifest.json`) maps each artifact file name to its SHA-256 and size.
Entries are recorded at first download (`gate.models`) and never rewritten by a later run:
a file whose hash differs from its entry fails verification.

CLI: `python -m gate.manifest verify [--config serving/config.yaml] [--all]` prints a JSON
result and exits 1 when any checked artifact is missing or differs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import yaml

SCHEMA_VERSION = 1
DEFAULT_MANIFEST = Path("serving/manifest.json")
DEFAULT_MODELS = Path("serving/models.yaml")
DEFAULT_CONFIG = Path("serving/config.yaml")
_CHUNK = 1 << 20


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def load_yaml(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict:
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "artifacts": {}}
    with open(path) as f:
        data = json.load(f)
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"{path}: unsupported manifest schema_version {data.get('schema_version')}"
        )
    return data


def save_manifest(manifest: dict, path: Path = DEFAULT_MANIFEST) -> None:
    manifest = manifest | {
        "schema_version": SCHEMA_VERSION,
        "artifacts": dict(sorted(manifest["artifacts"].items())),
    }
    path.write_text(json.dumps(manifest, indent=2) + "\n")


def check_artifact(manifest: dict, name: str, models_dir: Path) -> dict:
    """Check one artifact against the manifest. Returns a JSON-serialisable result."""
    entry = manifest["artifacts"].get(name)
    path = models_dir / name
    result = {"artifact": name, "path": str(path), "expected_sha256": None, "actual_sha256": None}
    if entry is None:
        return result | {"ok": False, "reason": "not in manifest"}
    result["expected_sha256"] = entry["sha256"]
    if not path.exists():
        return result | {"ok": False, "reason": "file missing"}
    actual = sha256_file(path)
    result["actual_sha256"] = actual
    if actual != entry["sha256"]:
        return result | {"ok": False, "reason": "sha256 mismatch"}
    return result | {"ok": True, "reason": None}


def verify_templates(manifest_path: Path = DEFAULT_MANIFEST) -> list[dict]:
    """Check the pinned templates (manifest `templates` section) against their files."""
    out = []
    for rel, entry in sorted(load_manifest(manifest_path).get("templates", {}).items()):
        path = Path(rel)
        actual = sha256_file(path) if path.exists() else None
        out.append(
            {
                "template": rel,
                "expected_sha256": entry["sha256"],
                "actual_sha256": actual,
                "ok": actual == entry["sha256"],
            }
        )
    return out


def verify(
    names: list[str], manifest_path: Path = DEFAULT_MANIFEST, models_dir: Path = Path("models")
) -> dict:
    manifest = load_manifest(manifest_path)
    checks = [check_artifact(manifest, n, models_dir) for n in names]
    return {
        "schema_version": SCHEMA_VERSION,
        "check": "manifest",
        "ok": bool(checks) and all(c["ok"] for c in checks),
        "artifacts": checks,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="gate.manifest", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify", help="verify artifacts against the manifest")
    v.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="serving config")
    v.add_argument("--models", type=Path, default=DEFAULT_MODELS, help="models.yaml")
    v.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    v.add_argument(
        "--all", action="store_true", help="check every manifest entry, not just the served model"
    )
    args = p.parse_args(argv)

    models_cfg = load_yaml(args.models)
    models_dir = Path(models_cfg["models_dir"])
    if args.all:
        names = sorted(load_manifest(args.manifest)["artifacts"])
    else:
        names = [load_yaml(args.config)["model"]]
    result = verify(names, args.manifest, models_dir)
    result["templates"] = verify_templates(args.manifest)
    result["ok"] = result["ok"] and all(t["ok"] for t in result["templates"])
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
